#!/usr/bin/env python3
"""
Script de teste para verificar a API do MarmoView
"""

import requests

def test_health():
    """Testa o endpoint de health check"""
    print("🔍 Testando health check...")
    try:
        response = requests.get('http://localhost:5000/api/health')
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Backend respondendo")
            print(f"   Status: {data['status']}")
            print(f"   Sessões ativas: {data['sessions_active']}")
            print(f"   Timestamp: {data['timestamp']}")
            return True
        else:
            print(f"❌ Erro: Status {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Erro: {e}")
        return False

def main():
    print("=" * 60)
    print("MarmoView - Teste de API")
    print("=" * 60)
    
    if test_health():
        print("\n✅ Sistema está funcionando!")
        print("\nPróximos passos:")
        print("1. Acesse http://localhost:5000 no navegador")
        print("2. Teste o upload de imagens pelo formulário")
        print("3. Verifique a geração do PDF")
    else:
        print("\n❌ Sistema com problemas")
        print("\nVerifique se o backend está rodando:")
        print("  python3 app.py")
    
    print("=" * 60)

if __name__ == '__main__':
    main()
