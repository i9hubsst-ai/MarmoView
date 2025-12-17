#!/usr/bin/env python3
"""
MarmoView Backend - Sistema de Upload e Geração de Desenhos
Sem persistência: dados são mantidos apenas em memória durante execução
"""

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename
import os
import io
import base64
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
from PIL import Image, ImageDraw, ImageFont
import uuid
import json
import requests

# Importar cliente OpenAI (usaremos mock se não estiver configurado)
try:
    from openai import OpenAI
    openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY', ''))
    HAS_OPENAI = bool(os.getenv('OPENAI_API_KEY'))
except:
    HAS_OPENAI = False
    openai_client = None

# Importar cliente Anthropic (Claude Vision)
try:
    from anthropic import Anthropic
    anthropic_client = Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY', ''))
    HAS_CLAUDE_VISION = bool(os.getenv('ANTHROPIC_API_KEY'))
except:
    HAS_CLAUDE_VISION = False
    anthropic_client = None

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# Armazenamento temporário em memória
# Quando o servidor reinicia, tudo é perdido
session_data = {}

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/api/upload', methods=['POST'])
def upload_files():
    """Recebe upload de imagens e dados do formulário"""
    
    if 'images' not in request.files:
        return jsonify({'error': 'Nenhuma imagem enviada'}), 400
    
    files = request.files.getlist('images')
    
    if len(files) == 0:
        return jsonify({'error': 'Lista de imagens vazia'}), 400
    
    if len(files) > 5:
        return jsonify({'error': 'Máximo de 5 imagens permitido'}), 400
    
    # Gera ID único para esta sessão
    session_id = str(uuid.uuid4())
    
    # Processa imagens
    images_data = []
    for file in files:
        if file and allowed_file(file.filename):
            # Lê imagem em memória
            img_bytes = file.read()
            
            if len(img_bytes) > MAX_FILE_SIZE:
                return jsonify({'error': f'Arquivo {file.filename} excede 10MB'}), 400
            
            # Converte para base64 para manter em memória
            img_b64 = base64.b64encode(img_bytes).decode('utf-8')
            
            # Extrai dimensões
            img = Image.open(io.BytesIO(img_bytes))
            width, height = img.size
            
            images_data.append({
                'filename': secure_filename(file.filename),
                'data': img_b64,
                'width': width,
                'height': height,
                'format': img.format
            })
    
    # Captura dados do formulário
    form_data = {
        'characteristics': request.form.get('characteristics', ''),
        'envType': request.form.get('envType', ''),
        'stoneElements': request.form.getlist('stoneElements'),
        'format': request.form.get('format', ''),
        'cutouts': request.form.getlist('cutouts'),
        'timestamp': datetime.now().isoformat()
    }
    
    # Armazena na sessão (memória)
    session_data[session_id] = {
        'images': images_data,
        'form': form_data,
        'status': 'uploaded'
    }
    
    return jsonify({
        'success': True,
        'session_id': session_id,
        'images_count': len(images_data),
        'message': f'{len(images_data)} imagem(ns) recebida(s) com sucesso'
    })

@app.route('/api/generate-drawing/<session_id>', methods=['POST'])
def generate_drawing(session_id):
    """Gera desenho conceitual baseado nas imagens e dados"""
    
    if session_id not in session_data:
        return jsonify({'error': 'Sessão não encontrada ou expirada'}), 404
    
    data = session_data[session_id]
    
    # Tenta análise com Claude Vision
    ai_analysis = analyze_images_with_claude(data['images'], data['form'])
    
    # Cria descrição do desenho (usa análise IA se disponível)
    drawing_description = create_conceptual_drawing(data, ai_analysis)
    
    # Gera imagem do desenho em memória
    drawing_image = generate_drawing_image(drawing_description, data, ai_analysis)
    
    # --- Integração Hugging Face Space para geração de imagem ---
    HF_SPACE_URL = os.getenv('HF_SPACE_URL')
    HF_TOKEN = os.getenv('HF_API_KEY')
    USE_HF_IMAGE = bool(HF_SPACE_URL)

    if USE_HF_IMAGE and data['images']:
        # Usa a primeira imagem enviada como base
        input_image_b64 = data['images'][0]['data']
        # Prompt pode ser detalhado a partir do drawing_description
        prompt = drawing_description.get('title', '') + ". " + drawing_description.get('characteristics', '')
        # Chama Hugging Face Space
        hf_img = generate_image_with_hf_space(input_image_b64, prompt, HF_SPACE_URL, HF_TOKEN)
        if hf_img:
            drawing_image = hf_img

    # Atualiza status
    session_data[session_id]['status'] = 'drawing_created'
    session_data[session_id]['drawing'] = drawing_description
    session_data[session_id]['drawing_image'] = drawing_image
    session_data[session_id]['ai_analysis'] = ai_analysis
    
    return jsonify({
        'success': True,
        'session_id': session_id,
        'drawing': drawing_description,
        'drawing_url': f'/api/drawing-image/{session_id}',
        'ai_analysis': ai_analysis,
        'message': 'Desenho conceitual gerado com sucesso'
    })

@app.route('/api/drawing-image/<session_id>', methods=['GET'])
def get_drawing_image(session_id):
    """Retorna a imagem do desenho gerado"""
    
    if session_id not in session_data:
        return jsonify({'error': 'Sessão não encontrada'}), 404
    
    if 'drawing_image' not in session_data[session_id]:
        return jsonify({'error': 'Desenho não foi gerado'}), 404
    
    img_data = session_data[session_id]['drawing_image']
    
    return send_file(
        io.BytesIO(img_data),
        mimetype='image/png',
        as_attachment=False
    )

def analyze_images_with_claude(images_data, form_data):
    """Analisa imagens com Claude Vision e retorna insights para o desenho"""
    
    if not HAS_CLAUDE_VISION:
        # Se Claude não estiver configurado, usa análise simbólica
        return None
    
    try:
        # Prepara imagens para Claude Vision
        image_contents = []
        for img_data in images_data[:3]:  # Máximo 3 imagens para não sobrecarregar
            # Claude aceita base64
            image_contents.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": img_data['data'],
                },
            })
        
        # Adiciona texto do prompt
        prompt_text = f"""Você é um especialista em marmoraria, design de interiores e desenho técnico para fabricação de pedras naturais.

CONTEXTO:
Analise esta(s) imagem(ns) de um ambiente {form_data.get('envType', 'não especificado')} que receberá revestimento em pedra natural.

DADOS DO FORMULÁRIO:
- Tipo de ambiente: {form_data.get('envType')}
- Formato desejado: {form_data.get('format')}
- Elementos de pedra: {', '.join(form_data.get('stoneElements', []))}
- Recortes necessários: {', '.join(form_data.get('cutouts', []))}
- Características descritas: {form_data.get('characteristics', 'Nenhuma')}

TAREFA:
Você deve fornecer uma análise EXTREMAMENTE DETALHADA para gerar um desenho técnico conceitual preciso.

Retorne um JSON com as seguintes chaves (todas obrigatórias):

1. "layout_analysis": Descrição DETALHADA do layout atual do espaço (paredes, móveis, estruturas visíveis)

2. "space_dimensions": Objeto com estimativas de proporções baseadas na imagem:
   - "width_ratio": largura aproximada em relação à altura (ex: 1.5 = 50% mais largo)
   - "depth_ratio": profundidade em relação à largura
   - "height_estimate": altura estimada em cm (padrão 240cm se não identificar)

3. "stone_layout": Objeto DETALHADO com posicionamento dos elementos de pedra:
   - "main_surface": descrição da superfície principal (bancada/parede/piso)
   - "positions": lista de objetos, cada um com:
     * "element": nome do elemento (bancada/ilha/nicho/etc)
     * "x_start": posição X inicial (0-100, porcentagem da largura)
     * "x_end": posição X final (0-100)
     * "y_start": posição Y inicial (0-100, porcentagem da altura)
     * "y_end": posição Y final (0-100)
     * "description": descrição do posicionamento

4. "cutouts_positions": lista de objetos para cada recorte identificado:
   - "type": tipo do recorte (pia/cooktop/torneira/etc)
   - "x": posição X (0-100)
   - "y": posição Y (0-100)
   - "size": tamanho estimado (pequeno/médio/grande)
   - "notes": observações sobre o recorte

5. "format_recommendation": Como o formato {form_data.get('format')} se encaixa no espaço analisado

6. "visual_references": Lista de elementos visuais chave identificados nas imagens (cores, texturas, estilo)

7. "drawing_instructions": Lista de instruções específicas para o desenho técnico (ex: "posicionar ilha centralizada", "bancada em L com 2.5m + 1.8m")

8. "challenges": Lista de desafios ou pontos de atenção identificados

9. "confidence": Nível de confiança da análise (0-100)

IMPORTANTE: 
- Seja MUITO ESPECÍFICO com posições e proporções
- Use as coordenadas 0-100 para facilitar o desenho
- Se não conseguir identificar algo nas imagens, use valores padrão razoáveis baseados no tipo de ambiente
- Responda APENAS com JSON válido, sem markdown ou explicações extras"""
        
        image_contents.append({
            "type": "text",
            "text": prompt_text
        })
        
        # Chama Claude Vision
        response = anthropic_client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": image_contents,
                }
            ],
        )
        
        # Extrai resposta de texto
        response_text = response.content[0].text
        
        # Tenta parsear JSON
        import json
        analysis = json.loads(response_text)
        return analysis
        
    except Exception as e:
        print(f"⚠️ Erro ao analisar com Claude Vision: {e}")
        return None

def create_conceptual_drawing(data, ai_analysis=None):
    """Cria descrição conceitual do desenho baseado nos dados e análise IA"""
    
    form = data['form']
    images_count = len(data['images'])
    
    # Mapeia tipos de ambiente
    env_types = {
        'cozinha': 'Cozinha Residencial',
        'cozinha-comercial': 'Cozinha Comercial',
        'banheiro': 'Banheiro',
        'area-gourmet': 'Área Gourmet',
        'lavabo': 'Lavabo',
        'sala': 'Sala',
        'varanda': 'Varanda/Sacada',
        'outro': 'Outro'
    }
    
    # Mapeia formatos
    formats = {
        'reto': 'Reto/Linear',
        'l': 'Em L',
        'u': 'Em U',
        'ilha': 'Ilha Central',
        'pensula': 'Península',
        'irregular': 'Irregular',
        'outro': 'Outro'
    }
    
    drawing = {
        'title': f'Desenho Conceitual - {env_types.get(form["envType"], "Não especificado")}',
        'environment': env_types.get(form['envType'], 'Não especificado'),
        'format': formats.get(form['format'], 'Não especificado'),
        'elements': form['stoneElements'],
        'cutouts': form['cutouts'],
        'characteristics': form['characteristics'],
        'images_analyzed': images_count,
        'shapes': generate_geometric_shapes(form),
        'ai_analysis': ai_analysis,  # Adiciona análise IA se disponível
        'notes': [
            'DESENHO CONCEITUAL - NÃO UTILIZAR PARA FABRICAÇÃO',
            'Requer medição precisa em campo',
            'Sem escala ou dimensões',
            'Representa interpretação visual do ambiente'
        ]
    }
    
    return drawing

def generate_geometric_shapes(form):
    """Gera formas geométricas básicas baseadas nos dados"""
    shapes = []
    
    # Forma principal baseada no formato
    if form['format'] == 'l':
        shapes.append({'type': 'L-shape', 'description': 'Configuração em L'})
    elif form['format'] == 'u':
        shapes.append({'type': 'U-shape', 'description': 'Configuração em U'})
    elif form['format'] == 'reto':
        shapes.append({'type': 'linear', 'description': 'Configuração linear'})
    elif form['format'] == 'ilha':
        shapes.append({'type': 'island', 'description': 'Ilha central destacada'})
    
    # Adiciona elementos
    for element in form['stoneElements']:
        if element == 'bancada':
            shapes.append({'type': 'rectangle', 'description': 'Bancada principal'})
        elif element == 'ilha':
            shapes.append({'type': 'rectangle', 'description': 'Ilha central'})
        elif element == 'nicho':
            shapes.append({'type': 'small-rect', 'description': 'Nicho/Prateleira'})
    
    return shapes

def generate_drawing_image(drawing, data, ai_analysis=None):
    """Gera imagem PNG do desenho conceitual com análise de IA"""
    from PIL import ImageDraw
    
    # Análise dos dados para desenho mais preciso
    form = data['form']
    
    # Cria canvas maior 1000x700
    img = Image.new('RGB', (1000, 700), color=(245, 245, 242))  # branco-marmore
    draw = ImageDraw.Draw(img)
    
    # Configuração de cores
    cor_principal = (110, 139, 127)  # jade
    cor_texto = (46, 46, 46)  # grafite
    cor_claro = (158, 158, 158)  # cinza-medio
    cor_borda = (46, 46, 46)
    
    # Título
    draw.text((40, 30), drawing['title'], fill=cor_texto)
    
    # Informações do projeto
    y = 70
    info_text = f"Ambiente: {drawing['environment']} | Formato: {drawing['format']}"
    draw.text((40, y), info_text, fill=cor_claro)
    
    y = 110
    elementos_text = f"Elementos: {', '.join(drawing['elements'][:3])}" if drawing['elements'] else "Elementos: Diversos"
    draw.text((40, y), elementos_text, fill=cor_claro)
    
    # Mostra se análise IA foi aplicada
    if ai_analysis and 'confidence' in ai_analysis:
        y = 135
        ai_confidence = ai_analysis.get('confidence', 0)
        draw.text((40, y), f"✓ Análise IA: {ai_confidence}% de confiança", fill=cor_principal)
        y_offset = 190  # Ajusta offset para texto da IA
    else:
        y_offset = 170  # Offset normal
    
    # Área principal de desenho
    canvas_height = 450
    canvas_width = 900
    
    # Desenha bordos da área de desenho
    draw.rectangle([40, y_offset, 40 + canvas_width, y_offset + canvas_height], 
                   outline=cor_borda, width=2)
    
    # Desenha grid sutil
    for i in range(0, canvas_width, 100):
        draw.line([40 + i, y_offset, 40 + i, y_offset + canvas_height], 
                 fill=(220, 220, 220), width=1)
    for i in range(0, canvas_height, 100):
        draw.line([40, y_offset + i, 40 + canvas_width, y_offset + i], 
                 fill=(220, 220, 220), width=1)
    
    # Desenha formas baseadas no formato e análise IA
    margin = 80
    
    if ai_analysis and 'stone_layout' in ai_analysis:
        # Usa posicionamento inteligente da IA
        draw_intelligent_layout(draw, ai_analysis, y_offset, canvas_width, canvas_height, cor_principal, cor_borda, cor_texto)
    else:
        # Fallback: desenho genérico
        draw_format_shapes(draw, form['format'], y_offset + margin, cor_principal, cor_borda)
        draw_stone_elements(draw, form['stoneElements'], y_offset + margin, cor_principal)
        draw_cutouts(draw, form['cutouts'], y_offset + margin, cor_texto)
    
    # Legenda de recortes
    y = y_offset + canvas_height + 30
    recortes_text = f"Recortes: {', '.join(form['cutouts'])}" if form['cutouts'] and form['cutouts'][0] != 'nenhum' else "Recortes: Não identificados"
    draw.text((40, y), recortes_text, fill=cor_claro)
    
    # Avisos legais
    y = y + 40
    draw.text((40, y), "⚠️ DESENHO CONCEITUAL - NÃO UTILIZAR PARA FABRICAÇÃO", fill=(164, 90, 82))
    y += 25
    draw.text((40, y), "Requer medição precisa em campo. Sem escala ou dimensões.", fill=cor_claro)
    
    # Rodapé
    draw.text((40, 680), "MarmoView v1.0.0 - Desenho técnico minimalista para marmoraria", 
             fill=(158, 158, 158))
    
    # Salva em buffer
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    
    return buffer.read()

def draw_intelligent_layout(draw, ai_analysis, y_offset, canvas_width, canvas_height, cor_principal, cor_borda, cor_texto):
    """Desenha layout inteligente baseado na análise da IA"""
    
    # Offset base para desenho (margem da área de desenho)
    x_base = 40
    y_base = y_offset
    
    try:
        stone_layout = ai_analysis.get('stone_layout', {})
        positions = stone_layout.get('positions', [])
        
        # Desenha cada elemento de pedra nas posições especificadas pela IA
        for pos in positions:
            element = pos.get('element', '')
            x_start = pos.get('x_start', 0)
            x_end = pos.get('x_end', 100)
            y_start = pos.get('y_start', 0)
            y_end = pos.get('y_end', 100)
            
            # Converte porcentagem para coordenadas reais
            x1 = x_base + int((x_start / 100) * canvas_width)
            x2 = x_base + int((x_end / 100) * canvas_width)
            y1 = y_base + int((y_start / 100) * canvas_height)
            y2 = y_base + int((y_end / 100) * canvas_height)
            
            # Desenha retângulo do elemento
            draw.rectangle([x1, y1, x2, y2], 
                          outline=cor_principal, width=4)
            
            # Adiciona label do elemento
            label_y = y1 - 20 if y1 > y_base + 30 else y1 + 5
            draw.text((x1 + 10, label_y), element.upper(), fill=cor_principal)
        
        # Desenha recortes nas posições especificadas
        cutouts = ai_analysis.get('cutouts_positions', [])
        for cutout in cutouts:
            cutout_type = cutout.get('type', '')
            x = cutout.get('x', 50)
            y = cutout.get('y', 50)
            size = cutout.get('size', 'médio')
            
            # Converte para coordenadas
            cx = x_base + int((x / 100) * canvas_width)
            cy = y_base + int((y / 100) * canvas_height)
            
            # Tamanho do círculo baseado no tipo
            radius = 15 if size == 'pequeno' else 25 if size == 'médio' else 35
            
            # Desenha círculo vermelho para recorte
            draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                        outline=(164, 90, 82), width=3, fill=(255, 200, 200, 128))
            
            # Label do recorte
            draw.text((cx + radius + 5, cy - 10), cutout_type[:3].upper(), fill=(164, 90, 82))
        
        # Adiciona notas da IA se houver
        drawing_instructions = ai_analysis.get('drawing_instructions', [])
        if drawing_instructions:
            y_note = y_base + canvas_height + 10
            note_text = " | ".join(drawing_instructions[:2])  # Primeiras 2 instruções
            if len(note_text) > 100:
                note_text = note_text[:97] + "..."
            draw.text((x_base, y_note), f"ℹ️ {note_text}", fill=(110, 139, 127))
            
    except Exception as e:
        print(f"⚠️ Erro ao desenhar layout inteligente: {e}")
        # Se falhar, não desenha nada (grid já foi desenhado)

def draw_format_shapes(draw, format_type, y_base, cor_principal, cor_borda):
    """Desenha formas baseadas no tipo de formato"""
    x_base = 120
    
    if format_type == 'Reto/Linear':
        # Linha reta simples
        draw.rectangle([x_base, y_base, x_base + 600, y_base + 120], 
                      outline=cor_principal, width=3)
    elif format_type == 'Em L':
        # Forma em L
        draw.rectangle([x_base, y_base, x_base + 350, y_base + 120], 
                      outline=cor_principal, width=3)
        draw.rectangle([x_base, y_base + 120, x_base + 150, y_base + 300], 
                      outline=cor_principal, width=3)
    elif format_type == 'Em U':
        # Forma em U
        draw.rectangle([x_base, y_base, x_base + 120, y_base + 280], 
                      outline=cor_principal, width=3)
        draw.rectangle([x_base + 120, y_base, x_base + 480, y_base + 120], 
                      outline=cor_principal, width=3)
        draw.rectangle([x_base + 360, y_base, x_base + 480, y_base + 280], 
                      outline=cor_principal, width=3)
    elif format_type == 'Ilha Central':
        # Ilha central
        draw.rectangle([x_base + 100, y_base + 80, x_base + 300, y_base + 200], 
                      outline=cor_principal, width=3)
        draw.rectangle([x_base, y_base, x_base + 400, y_base + 80], 
                      outline=cor_principal, width=2)
    elif format_type == 'Península':
        # Península
        draw.rectangle([x_base, y_base, x_base + 250, y_base + 120], 
                      outline=cor_principal, width=3)
        draw.rectangle([x_base + 200, y_base, x_base + 350, y_base + 250], 
                      outline=cor_principal, width=3)
    else:
        # Formato irregular
        pontos = [x_base, y_base, x_base + 300, y_base + 50, x_base + 450, y_base + 150,
                 x_base + 400, y_base + 280, x_base + 100, y_base + 250]
        draw.polygon(pontos, outline=cor_principal, fill=None, width=3)

def draw_stone_elements(draw, elements, y_base, cor_principal):
    """Desenha indicadores de elementos de pedra"""
    x_pos = 150
    y_pos = y_base + 200
    
    for i, element in enumerate(elements[:4]):
        if i > 0:
            x_pos += 120
        # Desenha círculo com indicador
        draw.ellipse([x_pos, y_pos, x_pos + 60, y_pos + 60], 
                    outline=cor_principal, width=2)

def draw_cutouts(draw, cutouts, y_base, cor_texto):
    """Desenha indicadores de recortes"""
    x_pos = 150
    y_pos = y_base + 300
    
    for i, cutout in enumerate(cutouts[:5]):
        if cutout == 'nenhum':
            continue
        if i > 0:
            x_pos += 90
        # Desenha pequena marca vermelha para recortes
        draw.ellipse([x_pos, y_pos, x_pos + 30, y_pos + 30], 
                    outline=(164, 90, 82), width=2, fill=(255, 200, 200))

@app.route('/api/generate-pdf/<session_id>', methods=['GET'])
def generate_pdf(session_id):
    """Gera PDF do desenho conceitual"""
    
    if session_id not in session_data:
        return jsonify({'error': 'Sessão não encontrada'}), 404
    
    data = session_data[session_id]
    
    if 'drawing' not in data:
        return jsonify({'error': 'Desenho não foi gerado ainda'}), 400
    
    # Cria PDF em memória
    pdf_buffer = io.BytesIO()
    c = canvas.Canvas(pdf_buffer, pagesize=A4)
    width, height = A4
    
    # Cabeçalho
    c.setFont("Helvetica-Bold", 16)
    c.drawString(2*cm, height - 2*cm, "MARMOVIEW")
    
    c.setFont("Helvetica", 10)
    c.drawString(2*cm, height - 2.5*cm, f"Projeto: #{session_id[:8]}")
    c.drawString(2*cm, height - 3*cm, f"Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    
    # Linha divisória
    c.line(2*cm, height - 3.5*cm, width - 2*cm, height - 3.5*cm)
    
    # Título do desenho
    drawing = data['drawing']
    c.setFont("Helvetica-Bold", 14)
    c.drawString(2*cm, height - 4.5*cm, drawing['title'])
    
    # Informações do ambiente
    y = height - 5.5*cm
    c.setFont("Helvetica", 10)
    c.drawString(2*cm, y, f"Ambiente: {drawing['environment']}")
    
    y -= 0.6*cm
    c.drawString(2*cm, y, f"Formato: {drawing['format']}")
    
    y -= 0.6*cm
    c.drawString(2*cm, y, f"Elementos: {', '.join(drawing['elements'])}")
    
    y -= 0.6*cm
    if drawing['cutouts']:
        c.drawString(2*cm, y, f"Recortes: {', '.join(drawing['cutouts'])}")
        y -= 0.6*cm
    
    # Área de desenho (placeholder)
    y -= 1*cm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(2*cm, y, "Desenho Conceitual:")
    
    y -= 1*cm
    # Desenha retângulo representando área de desenho
    c.rect(2*cm, y - 10*cm, width - 4*cm, 10*cm)
    
    # Formas geométricas simples
    c.setFont("Helvetica", 9)
    shapes_text = " | ".join([s['description'] for s in drawing['shapes']])
    c.drawString(2.5*cm, y - 5*cm, shapes_text)
    
    # Características descritas
    y = y - 11*cm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(2*cm, y, "Características Observadas:")
    
    y -= 0.6*cm
    c.setFont("Helvetica", 9)
    # Quebra texto em linhas
    char_text = drawing['characteristics'][:200]  # Limita caracteres
    c.drawString(2.5*cm, y, char_text)
    
    # Avisos legais
    y -= 2*cm
    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.7, 0, 0)
    c.drawString(2*cm, y, "AVISOS IMPORTANTES:")
    
    y -= 0.7*cm
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0, 0, 0)
    for note in drawing['notes']:
        c.drawString(2.5*cm, y, f"• {note}")
        y -= 0.5*cm
    
    # Rodapé
    c.setFont("Helvetica", 8)
    c.drawString(2*cm, 2*cm, "MarmoView v1.0.0 - Sistema IA para Marmoraria")
    c.drawString(2*cm, 1.5*cm, '"Quem mede, manda." - Desenho requer validacao em campo.')
    
    c.save()
    
    # Prepara para envio
    pdf_buffer.seek(0)
    
    # Atualiza status
    session_data[session_id]['status'] = 'pdf_generated'
    
    return send_file(
        pdf_buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'marmoview_desenho_{session_id[:8]}.pdf'
    )

@app.route('/api/session/<session_id>', methods=['GET'])
def get_session(session_id):
    """Retorna dados da sessão (sem as imagens base64 para economizar)"""
    
    if session_id not in session_data:
        return jsonify({'error': 'Sessão não encontrada'}), 404
    
    data = session_data[session_id].copy()
    
    # Remove dados pesados das imagens
    if 'images' in data:
        data['images'] = [
            {'filename': img['filename'], 'width': img['width'], 'height': img['height']}
            for img in data['images']
        ]
    
    return jsonify(data)

@app.route('/api/health', methods=['GET'])
def health_check():
    """Endpoint de health check"""
    return jsonify({
        'status': 'ok',
        'sessions_active': len(session_data),
        'timestamp': datetime.now().isoformat()
    })

def generate_image_with_hf_space(input_image_b64, prompt, hf_space_url, hf_token=None):
    """
    Envia imagem base64 + prompt para um Space Hugging Face (ex: ControlNet, SDXL img2img)
    Retorna bytes da imagem gerada ou None em caso de erro.
    """
    try:
        # Decodifica imagem base64 para bytes
        image_bytes = base64.b64decode(input_image_b64)
        files = {"image": ("input.png", image_bytes, "image/png")}
        data = {"prompt": prompt}
        headers = {}
        if hf_token:
            headers["Authorization"] = f"Bearer {hf_token}"
        # Alguns Spaces usam /api/predict, outros /run/predict, outros / (root)
        # Exemplo: https://huggingface.co/spaces/hysts/ControlNet
        response = requests.post(hf_space_url, data=data, files=files, headers=headers, timeout=120)
        if response.status_code == 200:
            # Pode retornar JSON com url ou bytes diretos
            if "application/json" in response.headers.get("Content-Type", ""):
                result = response.json()
                # Tenta pegar url ou base64
                if "image" in result:
                    # Pode ser base64
                    return base64.b64decode(result["image"])
                elif "url" in result:
                    # Baixa a imagem
                    img_resp = requests.get(result["url"])
                    if img_resp.status_code == 200:
                        return img_resp.content
            else:
                # Retorno direto da imagem
                return response.content
        else:
            print(f"[HF Space] Status: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"[HF Space] Erro: {e}")
    return None

if __name__ == '__main__':
    print("=" * 60)
    print("MarmoView Backend - Iniciando...")
    print("=" * 60)
    print("✓ Sem persistência: dados em memória")
    print("✓ Upload de imagens ativo")
    print("✓ Geração de PDF ativo")
    print("=" * 60)
    print("Acesse: http://localhost:5000")
    print("=" * 60)
    
    app.run(debug=True, host='0.0.0.0', port=5000)
