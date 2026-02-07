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

# Carrega variáveis de ambiente do arquivo .env
from dotenv import load_dotenv
load_dotenv()
print("[CONFIG] Arquivo .env carregado")

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
    
    # --- Prioridade: OpenAI DALL-E 3 > Hugging Face > Desenho Local ---
    
    # OPÇÃO 1: Tentar OpenAI DALL-E 3 primeiro (melhor qualidade)
    if HAS_OPENAI and data['images']:
        try:
            # Cria prompt técnico detalhado
            env_map = {
                'cozinha': 'kitchen countertop',
                'banheiro': 'bathroom vanity',
                'area-gourmet': 'gourmet area',
                'lavabo': 'powder room',
                'outro': 'interior space'
            }
            
            format_map = {
                'reto': 'linear straight layout',
                'l': 'L-shaped layout',
                'u': 'U-shaped layout',
                'ilha': 'island configuration',
                'pensula': 'peninsula layout',
                'irregular': 'custom irregular shape'
            }
            
            env_desc = env_map.get(data['form']['envType'], 'interior space')
            format_desc = format_map.get(data['form']['format'], 'custom layout')
            
            # Prompt otimizado para DALL-E 3
            prompt = f"Professional technical architectural blueprint drawing of {env_desc} with {format_desc}. "
            prompt += f"Top-down view, marble or granite countertop installation layout. "
            prompt += f"Clean lines, precise measurements indicators, professional CAD style, "
            prompt += f"minimalist design, high quality technical illustration with detailed stone placement"
            
            print(f"[OpenAI] Gerando imagem com DALL-E 3...")
            print(f"[OpenAI] Prompt: {prompt[:100]}...")
            
            dalle_img = generate_image_with_dalle(prompt)
            if dalle_img:
                print("[OpenAI] ✓ Imagem gerada com sucesso via DALL-E 3")
                drawing_image = dalle_img
            else:
                print("[OpenAI] ⚠️ DALL-E 3 falhou, tentando alternativas...")
        except Exception as e:
            print(f"[OpenAI] ⚠️ Erro: {e}")
    
    # OPÇÃO 2: Se OpenAI falhou/indisponível, tentar Hugging Face
    HF_SPACE_URL = os.getenv('HF_SPACE_URL')
    HF_TOKEN = os.getenv('HF_API_KEY')
    USE_HF_IMAGE = bool(HF_SPACE_URL) and not HAS_OPENAI  # Só tenta HF se OpenAI não estiver configurado

    if USE_HF_IMAGE and data['images']:
        try:
            # Usa a primeira imagem enviada como base
            input_image_b64 = data['images'][0]['data']
            
            # Cria prompt técnico detalhado para melhor resultado
            env_map = {
                'cozinha': 'kitchen countertop',
                'banheiro': 'bathroom vanity',
                'area-gourmet': 'gourmet area',
                'lavabo': 'powder room',
                'outro': 'interior space'
            }
            
            format_map = {
                'reto': 'linear straight layout',
                'l': 'L-shaped layout',
                'u': 'U-shaped layout',
                'ilha': 'island configuration',
                'pensula': 'peninsula layout',
                'irregular': 'custom irregular shape'
            }
            
            env_desc = env_map.get(data['form']['envType'], 'interior space')
            format_desc = format_map.get(data['form']['format'], 'custom layout')
            
            # Prompt otimizado para desenho técnico
            prompt = f"Technical architectural drawing of {env_desc} with {format_desc}, "
            prompt += f"marble or granite countertop installation, "
            prompt += f"professional blueprint style, clean lines, top-down view, "
            prompt += f"precise measurements indication, technical illustration, "
            prompt += f"high quality architectural rendering, detailed stone layout"
            
            print(f"[HF] Prompt técnico gerado: {prompt}")
            
            # Chama Hugging Face Space
            print(f"[HF] Tentando gerar imagem com HF Space: {HF_SPACE_URL}")
            hf_img = generate_image_with_hf_space(input_image_b64, prompt, HF_SPACE_URL, HF_TOKEN)
            if hf_img:
                print("[HF] ✓ Imagem gerada com sucesso via Hugging Face")
                drawing_image = hf_img
            else:
                print("[HF] ⚠️ Hugging Face falhou, usando desenho conceitual local")
        except Exception as e:
            print(f"[HF] ⚠️ Erro ao tentar Hugging Face: {e}. Usando desenho conceitual local")

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
    """Gera imagem PNG do desenho conceitual com análise de IA - VERSÃO MELHORADA"""
    from PIL import ImageDraw, ImageFont
    
    # Análise dos dados para desenho mais preciso
    form = data['form']
    
    # Cria canvas maior com proporção melhor - 1200x800
    img = Image.new('RGB', (1200, 800), color=(255, 255, 255))  # fundo branco puro
    draw = ImageDraw.Draw(img)
    
    # Configuração de cores profissionais
    cor_principal = (70, 100, 90)  # verde-escuro elegante
    cor_secundaria = (120, 160, 140)  # verde-médio
    cor_texto = (30, 30, 30)  # quase preto
    cor_titulo = (50, 50, 50)  # cinza escuro
    cor_grid = (240, 240, 240)  # cinza muito claro
    cor_recorte = (200, 50, 50)  # vermelho para recortes
    cor_medida = (100, 100, 180)  # azul para medidas
    
    # === CABEÇALHO ===
    try:
        # Tenta usar fonte TrueType, senão usa padrão
        font_titulo = ImageFont.truetype("arial.ttf", 24)
        font_subtitulo = ImageFont.truetype("arial.ttf", 16)
        font_texto = ImageFont.truetype("arial.ttf", 12)
        font_nota = ImageFont.truetype("arial.ttf", 10)
    except:
        font_titulo = ImageFont.load_default()
        font_subtitulo = ImageFont.load_default()
        font_texto = ImageFont.load_default()
        font_nota = ImageFont.load_default()
    
    # Título
    draw.text((30, 20), "MARMOVIEW - DESENHO CONCEITUAL", fill=cor_titulo, font=font_titulo)
    
    # Linha divisória
    draw.line([(30, 55), (1170, 55)], fill=cor_grid, width=2)
    
    # Informações do projeto
    y = 70
    info_text = f"Ambiente: {drawing['environment'].upper()}"
    draw.text((30, y), info_text, fill=cor_texto, font=font_subtitulo)
    
    y = 95
    formato_text = f"Configuração: {drawing['format']}"
    draw.text((30, y), formato_text, fill=cor_texto, font=font_texto)
    
    # Elementos identificados
    if drawing['elements'] and drawing['elements'][0] != 'nenhum':
        y = 115
        elementos = ', '.join([e.capitalize() for e in drawing['elements'][:5]])
        draw.text((30, y), f"Elementos: {elementos}", fill=cor_secundaria, font=font_texto)
    
    # Mostra se análise IA foi aplicada
    if ai_analysis and 'confidence' in ai_analysis:
        y = 135
        ai_confidence = ai_analysis.get('confidence', 0)
        draw.text((30, y), f"✓ Análise IA aplicada - Confiança: {ai_confidence}%", 
                 fill=cor_principal, font=font_texto)
        y_offset = 170
    else:
        y = 135
        draw.text((30, y), "⚠ Desenho baseado em formulário (sem análise de IA)", 
                 fill=(150, 150, 150), font=font_nota)
        y_offset = 160
    
    # === ÁREA DE DESENHO PRINCIPAL ===
    canvas_x = 50
    canvas_y = y_offset
    canvas_width = 1100
    canvas_height = 500
    
    # Fundo da área de desenho
    draw.rectangle([canvas_x, canvas_y, canvas_x + canvas_width, canvas_y + canvas_height], 
                   fill=(250, 250, 250), outline=cor_titulo, width=2)
    
    # Grid profissional mais sutil
    grid_spacing = 50
    for i in range(0, canvas_width, grid_spacing):
        draw.line([canvas_x + i, canvas_y, canvas_x + i, canvas_y + canvas_height], 
                 fill=cor_grid, width=1)
    for i in range(0, canvas_height, grid_spacing):
        draw.line([canvas_x, canvas_y + i, canvas_x + canvas_width, canvas_y + i], 
                 fill=cor_grid, width=1)
    
    # === DESENHO DA CONFIGURAÇÃO ===
    margin_x = 100
    margin_y = 50
    drawing_area_width = canvas_width - 2 * margin_x
    drawing_area_height = canvas_height - 2 * margin_y
    base_x = canvas_x + margin_x
    base_y = canvas_y + margin_y
    
    # Desenha baseado na configuração e análise IA
    if ai_analysis and 'stone_layout' in ai_analysis:
        draw_intelligent_layout(draw, ai_analysis, canvas_y, canvas_width, canvas_height, 
                               cor_principal, cor_titulo, cor_texto, canvas_x, font_texto)
    else:
        # Desenho melhorado baseado no formato
        draw_improved_format(draw, form['format'], base_x, base_y, drawing_area_width, 
                           drawing_area_height, cor_principal, cor_secundaria, cor_titulo, font_texto)
        
        # Adiciona elementos de pedra
        draw_improved_elements(draw, form['stoneElements'], base_x, base_y, 
                              drawing_area_width, drawing_area_height, cor_secundaria, font_nota)
        
        # Adiciona recortes
        draw_improved_cutouts(draw, form['cutouts'], base_x, base_y, 
                            drawing_area_width, drawing_area_height, cor_recorte, font_nota)
    
    # === INFORMAÇÕES ADICIONAIS ===
    y = canvas_y + canvas_height + 20
    
    # Recortes identificados
    if drawing['cutouts'] and drawing['cutouts'][0] != 'nenhum':
        recortes = ', '.join([c.capitalize() for c in drawing['cutouts'][:5]])
        draw.text((canvas_x, y), f"Recortes previstos: {recortes}", 
                 fill=cor_recorte, font=font_texto)
        y += 20
    
    # === AVISOS IMPORTANTES ===
    y += 10
    draw.rectangle([canvas_x, y, canvas_x + canvas_width, y + 60], 
                   fill=(255, 245, 240), outline=cor_recorte, width=2)
    
    y += 10
    draw.text((canvas_x + 20, y), "⚠️  IMPORTANTE - DESENHO CONCEITUAL", 
             fill=cor_recorte, font=font_subtitulo)
    y += 25
    draw.text((canvas_x + 20, y), 
             "• Não utilizar para fabricação • Requer medição precisa em campo • Sem escala exata", 
             fill=cor_texto, font=font_nota)
    
    # === RODAPÉ ===
    draw.text((30, 775), f"MarmoView v1.0 - Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}", 
             fill=(180, 180, 180), font=font_nota)
    draw.text((900, 775), f"Sessão: {data.get('session_id', 'N/A')[:12]}", 
             fill=(180, 180, 180), font=font_nota)
    
    # Salva em buffer
    buffer = io.BytesIO()
    img.save(buffer, format='PNG', quality=95)
    buffer.seek(0)
    
    return buffer.read()

def draw_improved_format(draw, format_type, base_x, base_y, width, height, cor_principal, cor_secundaria, cor_borda, font):
    """Desenha configuração de formato melhorada e proporcional"""
    
    # Mapeamento de formatos
    format_map = {
        'reto': 'Reto/Linear',
        'l': 'Em L',
        'u': 'Em U',
        'ilha': 'Ilha Central',
        'pensula': 'Península',
        'irregular': 'Irregular'
    }
    
    if format_type == 'reto':
        # Bancada linear - ocupa 80% da largura
        w = int(width * 0.8)
        h = int(height * 0.25)
        x = base_x + (width - w) // 2
        y = base_y + height // 3
        
        # Bancada principal
        draw.rectangle([x, y, x + w, y + h], outline=cor_principal, fill=cor_secundaria, width=4)
        draw.text((x + 10, y + 10), "BANCADA", fill=cor_borda, font=font)
        
        # Linha de parede atrás
        draw.line([base_x, y - 20, base_x + width, y - 20], fill=cor_borda, width=3)
        draw.text((base_x + 10, y - 35), "PAREDE", fill=(150, 150, 150), font=font)
        
    elif format_type == 'l':
        # Configuração em L
        # Bancada horizontal
        w1 = int(width * 0.6)
        h1 = int(height * 0.2)
        x1 = base_x + 50
        y1 = base_y + 50
        draw.rectangle([x1, y1, x1 + w1, y1 + h1], outline=cor_principal, fill=cor_secundaria, width=4)
        draw.text((x1 + 10, y1 + 10), "BANCADA 1", fill=cor_borda, font=font)
        
        # Bancada vertical (perpendicular)
        w2 = int(height * 0.2)
        h2 = int(height * 0.5)
        x2 = x1
        y2 = y1 + h1
        draw.rectangle([x2, y2, x2 + w2, y2 + h2], outline=cor_principal, fill=cor_secundaria, width=4)
        draw.text((x2 + 10, y2 + 20), "BANCADA 2", fill=cor_borda, font=font)
        
        # Paredes
        draw.line([base_x, y1 - 15, base_x + width, y1 - 15], fill=cor_borda, width=2)
        draw.line([x2 - 15, y1, x2 - 15, base_y + height], fill=cor_borda, width=2)
        
    elif format_type == 'u':
        # Configuração em U
        espessura = int(height * 0.18)
        
        # Bancada direita
        x1 = base_x + 50
        y1 = base_y + 40
        h1 = int(height * 0.7)
        draw.rectangle([x1, y1, x1 + espessura, y1 + h1], 
                      outline=cor_principal, fill=cor_secundaria, width=4)
        draw.text((x1 + 5, y1 + 20), "BANC.\nLAT.", fill=cor_borda, font=font)
        
        # Bancada central (fundo)
        x2 = x1
        y2 = y1
        w2 = int(width * 0.7)
        draw.rectangle([x2, y2, x2 + w2, y2 + espessura], 
                      outline=cor_principal, fill=cor_secundaria, width=4)
        draw.text((x2 + w2//2 - 30, y2 + 10), "BANCADA FUNDO", fill=cor_borda, font=font)
        
        # Bancada esquerda
        x3 = x2 + w2 - espessura
        y3 = y2
        draw.rectangle([x3, y3, x3 + espessura, y3 + h1], 
                      outline=cor_principal, fill=cor_secundaria, width=4)
        draw.text((x3 + 5, y3 + 20), "BANC.\nLAT.", fill=cor_borda, font=font)
        
    elif format_type == 'ilha':
        # Ilha central com bancadas laterais
        # Ilha no centro
        ilha_w = int(width * 0.4)
        ilha_h = int(height * 0.35)
        ilha_x = base_x + (width - ilha_w) // 2
        ilha_y = base_y + (height - ilha_h) // 2
        draw.rectangle([ilha_x, ilha_y, ilha_x + ilha_w, ilha_y + ilha_h], 
                      outline=cor_principal, fill=cor_secundaria, width=5)
        draw.text((ilha_x + ilha_w//2 - 20, ilha_y + ilha_h//2), "ILHA", fill=cor_borda, font=font)
        
        # Bancada na parede
        banc_w = int(width * 0.6)
        banc_h = int(height * 0.15)
        banc_x = base_x + (width - banc_w) // 2
        banc_y = base_y + 30
        draw.rectangle([banc_x, banc_y, banc_x + banc_w, banc_y + banc_h], 
                      outline=cor_secundaria, fill=(200, 220, 210), width=3)
        draw.text((banc_x + 10, banc_y + 5), "BANCADA PAREDE", fill=cor_borda, font=font)
        
    elif format_type == 'pensula':
        # Península - bancada principal + extensão
        # Bancada na parede
        w1 = int(width * 0.7)
        h1 = int(height * 0.2)
        x1 = base_x + 40
        y1 = base_y + 40
        draw.rectangle([x1, y1, x1 + w1, y1 + h1], 
                      outline=cor_principal, fill=cor_secundaria, width=4)
        draw.text((x1 + 10, y1 + 10), "BANCADA PRINCIPAL", fill=cor_borda, font=font)
        
        # Península (perpendicular)
        w2 = int(height * 0.25)
        h2 = int(height * 0.45)
        x2 = x1 + w1 - w2
        y2 = y1 + h1
        draw.rectangle([x2, y2, x2 + w2, y2 + h2], 
                      outline=cor_principal, fill=cor_secundaria, width=4)
        draw.text((x2 + 10, y2 + 20), "PENÍNSULA", fill=cor_borda, font=font)
        
    else:
        # Formato irregular - polígono assimétrico
        points = [
            (base_x + 100, base_y + 80),
            (base_x + width - 150, base_y + 50),
            (base_x + width - 100, base_y + height - 150),
            (base_x + width - 250, base_y + height - 80),
            (base_x + 80, base_y + height - 100)
        ]
        draw.polygon(points, outline=cor_principal, fill=cor_secundaria, width=4)
        draw.text((base_x + width//2 - 50, base_y + height//2), 
                 "FORMATO IRREGULAR", fill=cor_borda, font=font)

def draw_improved_elements(draw, elements, base_x, base_y, width, height, cor, font):
    """Desenha elementos de pedra melhorados"""
    if not elements or elements[0] == 'nenhum':
        return
    
    # Posiciona elementos de forma distribuída
    element_positions = {
        'bancada': (base_x + width * 0.3, base_y + height * 0.3),
        'pia': (base_x + width * 0.5, base_y + height * 0.4),
        'cooktop': (base_x + width * 0.6, base_y + height * 0.35),
        'mesa': (base_x + width * 0.4, base_y + height * 0.6),
        'soleira': (base_x + width * 0.7, base_y + height * 0.2)
    }
    
    for element in elements[:4]:
        if element in element_positions:
            x, y = element_positions[element]
            # Desenha ícone do elemento
            draw.ellipse([x-25, y-25, x+25, y+25], outline=cor, width=3)
            draw.text((x-20, y-5), element[:3].upper(), fill=cor, font=font)

def draw_improved_cutouts(draw, cutouts, base_x, base_y, width, height, cor, font):
    """Desenha recortes melhorados"""
    if not cutouts or cutouts[0] == 'nenhum':
        return
    
    # Posiciona recortes estrategicamente
    cutout_positions = []
    spacing_x = width // (len(cutouts) + 1)
    
    for i, cutout in enumerate(cutouts[:5]):
        x = base_x + spacing_x * (i + 1)
        y = base_y + height * 0.4
        
        # Desenha marca de recorte
        size = 20
        draw.ellipse([x-size, y-size, x+size, y+size], 
                    outline=cor, fill=(255, 220, 220), width=3)
        
        # Label
        label = cutout[:4].upper() if cutout != 'nenhum' else ''
        draw.text((x-15, y-8), label, fill=cor, font=font)

def draw_intelligent_layout(draw, ai_analysis, canvas_y, canvas_width, canvas_height, cor_principal, cor_borda, cor_texto, canvas_x, font):
    """Desenha layout inteligente baseado na análise da IA"""
    
    # Offset base para desenho (margem da área de desenho)
    x_base = canvas_x
    y_base = canvas_y
    
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
            
            # Desenha retângulo do elemento com preenchimento
            draw.rectangle([x1, y1, x2, y2], 
                          outline=cor_principal, fill=(200, 220, 210), width=4)
            
            # Adiciona label do elemento
            label_y = y1 - 20 if y1 > y_base + 30 else y1 + 5
            draw.text((x1 + 10, label_y), element.upper(), fill=cor_principal, font=font)
        
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
                        outline=(200, 50, 50), fill=(255, 200, 200), width=3)
            
            # Label do recorte
            draw.text((cx + radius + 5, cy - 10), cutout_type[:3].upper(), 
                     fill=(200, 50, 50), font=font)
        
        # Adiciona notas da IA se houver
        drawing_instructions = ai_analysis.get('drawing_instructions', [])
        if drawing_instructions:
            y_note = y_base + canvas_height + 10
            note_text = " | ".join(drawing_instructions[:2])  # Primeiras 2 instruções
            if len(note_text) > 100:
                note_text = note_text[:97] + "..."
            draw.text((x_base, y_note), f"ℹ️ {note_text}", fill=cor_principal, font=font)
            
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

def generate_image_with_dalle(prompt, size="1024x1024", quality="standard"):
    """
    Gera imagem usando OpenAI DALL-E 3
    Retorna bytes da imagem gerada ou None em caso de erro.
    
    Parâmetros:
    - prompt: Descrição da imagem desejada
    - size: "1024x1024", "1024x1792", ou "1792x1024"
    - quality: "standard" (~$0.04) ou "hd" (~$0.08)
    """
    try:
        from openai import OpenAI
        
        print(f"[DALL-E] Iniciando geração de imagem...")
        print(f"[DALL-E] Tamanho: {size}, Qualidade: {quality}")
        
        client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        
        # Gera imagem
        response = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size=size,
            quality=quality,
            n=1
        )
        
        # Pega URL da imagem gerada
        image_url = response.data[0].url
        print(f"[DALL-E] ✓ Imagem gerada: {image_url[:50]}...")
        
        # Baixa a imagem
        img_response = requests.get(image_url, timeout=60)
        if img_response.status_code == 200:
            print(f"[DALL-E] ✓ Imagem baixada ({len(img_response.content)} bytes)")
            return img_response.content
        else:
            print(f"[DALL-E] ⚠️ Erro ao baixar imagem: {img_response.status_code}")
            
    except Exception as e:
        print(f"[DALL-E] ⚠️ Erro: {e}")
        import traceback
        traceback.print_exc()
    
    return None

def generate_image_with_hf_space(input_image_b64, prompt, hf_space_url, hf_token=None):
    """
    Envia imagem base64 + prompt para um Space Hugging Face usando Gradio Client
    Suporta tanto URL do space quanto nome do repositório
    Retorna bytes da imagem gerada ou None em caso de erro.
    """
    try:
        # Importa gradio_client
        try:
            from gradio_client import Client
            print("[HF] Gradio Client disponível")
        except ImportError:
            print("[HF] ⚠️ gradio_client não instalado. Tentando método HTTP direto...")
            return _generate_image_http_fallback(input_image_b64, prompt, hf_space_url, hf_token)
        
        print(f"[HF] Conectando ao Space: {hf_space_url}")
        print(f"[HF] Prompt: {prompt[:100]}...")
        
        # Converte URL para formato correto
        # Se for URL completa, extrai o nome do space
        space_name = hf_space_url
        if "huggingface.co/spaces/" in hf_space_url:
            # Extrai: https://huggingface.co/spaces/usuario/modelo -> usuario/modelo
            space_name = hf_space_url.split("/spaces/")[-1].strip("/")
            print(f"[HF] Space detectado: {space_name}")
        elif ".hf.space" in hf_space_url:
            # URL direta do space - usa como está
            space_name = hf_space_url
        
        # Conecta ao Space
        if hf_token:
            client = Client(space_name, headers={"Authorization": f"Bearer {hf_token}"})
        else:
            client = Client(space_name)
        print(f"[HF] ✓ Conectado ao Space")
        
        # Decodifica imagem base64 para salvar temporariamente
        image_bytes = base64.b64decode(input_image_b64)
        
        # Salva temporariamente
        temp_path = f"temp_input_{uuid.uuid4()}.png"
        with open(temp_path, 'wb') as f:
            f.write(image_bytes)
        
        print(f"[HF] Enviando imagem e prompt para processamento...")
        
        # Tenta prever com diferentes assinaturas comuns
        result = None
        try:
            # Tenta primeiro com imagem + prompt (mais comum para img2img)
            result = client.predict(temp_path, prompt, api_name="/predict")
        except:
            try:
                # Tenta sem api_name
                result = client.predict(temp_path, prompt)
            except:
                try:
                    # Tenta apenas com imagem
                    result = client.predict(temp_path)
                except Exception as e:
                    print(f"[HF] ⚠️ Erro na predição: {e}")
                    # Remove arquivo temporário
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    return None
        
        # Remove arquivo temporário
        if os.path.exists(temp_path):
            os.remove(temp_path)
        
        if result:
            print(f"[HF] ✓ Resposta recebida: {type(result)}")
            
            # Result pode ser caminho de arquivo, URL ou bytes
            if isinstance(result, str):
                if os.path.exists(result):
                    # É um arquivo local
                    print(f"[HF] Lendo arquivo gerado: {result}")
                    with open(result, 'rb') as f:
                        return f.read()
                elif result.startswith('http'):
                    # É uma URL
                    print(f"[HF] Baixando de URL: {result}")
                    img_resp = requests.get(result)
                    if img_resp.status_code == 200:
                        return img_resp.content
            elif isinstance(result, bytes):
                print("[HF] ✓ Imagem recebida como bytes")
                return result
            elif isinstance(result, (list, tuple)) and len(result) > 0:
                # Gradio às vezes retorna lista
                img_path = result[0] if isinstance(result[0], str) else result
                if isinstance(img_path, str) and os.path.exists(img_path):
                    print(f"[HF] Lendo arquivo da lista: {img_path}")
                    with open(img_path, 'rb') as f:
                        return f.read()
        
        print("[HF] ⚠️ Não foi possível extrair imagem do resultado")
        
    except Exception as e:
        print(f"[HF] ⚠️ Exceção: {e}")
        import traceback
        traceback.print_exc()
    
    return None

def _generate_image_http_fallback(input_image_b64, prompt, hf_space_url, hf_token=None):
    """Método HTTP fallback quando Gradio Client não está disponível"""
    try:
        print(f"[HF] Tentando método HTTP para: {hf_space_url}")
        
        # Converte para URL da API se necessário
        api_url = hf_space_url
        if "huggingface.co/spaces/" in hf_space_url:
            # Converte para formato .hf.space
            space_name = hf_space_url.split("/spaces/")[-1].strip("/")
            username, model = space_name.split("/")
            api_url = f"https://{username}-{model}.hf.space/api/predict"
            print(f"[HF] URL da API: {api_url}")
        elif not api_url.endswith("/api/predict"):
            api_url = api_url.rstrip("/") + "/api/predict"
        
        # Decodifica imagem
        image_bytes = base64.b64decode(input_image_b64)
        
        # Payload para Gradio API
        payload = {
            "data": [
                f"data:image/png;base64,{input_image_b64}",
                prompt
            ]
        }
        
        headers = {"Content-Type": "application/json"}
        if hf_token:
            headers["Authorization"] = f"Bearer {hf_token}"
        
        response = requests.post(api_url, json=payload, headers=headers, timeout=120)
        
        print(f"[HF] Status HTTP: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            if "data" in result and len(result["data"]) > 0:
                img_data = result["data"][0]
                if isinstance(img_data, str):
                    if img_data.startswith("data:image"):
                        # Base64 embutido
                        img_b64 = img_data.split(",")[1]
                        return base64.b64decode(img_b64)
                    elif img_data.startswith("http"):
                        # URL
                        img_resp = requests.get(img_data)
                        if img_resp.status_code == 200:
                            return img_resp.content
        else:
            print(f"[HF] Erro: {response.text[:300]}")
            
    except Exception as e:
        print(f"[HF] Erro no fallback HTTP: {e}")
    
    return None

if __name__ == '__main__':
    print("=" * 60)
    print("MarmoView Backend - Iniciando...")
    print("=" * 60)
    print("✓ Sem persistência: dados em memória")
    print("✓ Upload de imagens ativo")
    print("✓ Geração de PDF ativo")
    print("=" * 60)
    
    # Status de integração com IAs
    print("Status de Integrações IA:")
    if HAS_CLAUDE_VISION:
        print("  ✓ Claude Vision: ATIVO (análise de imagens)")
    else:
        print("  ✗ Claude Vision: INATIVO (configure ANTHROPIC_API_KEY)")
    
    if HAS_OPENAI:
        print("  ✓ OpenAI: ATIVO")
    else:
        print("  ✗ OpenAI: INATIVO (configure OPENAI_API_KEY)")
    
    hf_url = os.getenv('HF_SPACE_URL')
    if hf_url:
        print(f"  ⚠️  Hugging Face Space: {hf_url}")
        print("      (Verifique se a URL está correta)")
    else:
        print("  ✗ Hugging Face: INATIVO")
    
    print("\n  ℹ️  Desenhos conceituais locais: SEMPRE DISPONÍVEL")
    print("=" * 60)
    print("Acesse: http://localhost:5000")
    print("=" * 60)
    
    app.run(debug=True, host='0.0.0.0', port=5000)
