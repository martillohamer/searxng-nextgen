import socket
import socks
import requests
from urllib3.contrib.socks import SOCKSProxyManager

class TorSocksClient:
    def __init__(self, socks_host: str = "127.0.0.1", socks_port: int = 9050):
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.session = requests.Session()
        self._configure_socks()

    def _configure_socks(self):
        """Configura la sesión HTTP para enrutar todo el tráfico a través de Tor SOCKS5h."""
        socks_url = f"socks5h://{self.socks_host}:{self.socks_port}"
        
        # Asignar adaptadores de transporte para SOCKS5h (evita fugas de DNS locales)
        self.session.proxies = {
            'http': socks_url,
            'https': socks_url
        }
        
        # Ajustar headers estándar para mantener anonimato
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0',
            'Accept-Language': 'en-US,en;q=0.5'
        })

    def get(self, url: str, timeout: int = 15) -> requests.Response:
        """Realiza una petición GET anónima a través de la red Tor."""
        try:
            response = self.session.get(url, timeout=timeout)
            return response
        except Exception as e:
            print(f"[Tor Error] Fallo al consultar {url} via Tor -> {e}")
            raise

    def rotate_circuit(self, control_port: int = 9051, password: str = None):
        """Solicita un nuevo circuito (NEWNYM) al daemon de Tor para cambiar la IP de salida."""
        try:
            with socket.create_connection((self.socks_host, control_port)) as s:
                if password:
                    s.sendall(f'AUTHENTICATE "{password}"\r\n'.encode('utf-8'))
                else:
                    s.sendall(b'AUTHENTICATE ""\r\n'.encode('utf-8'))
                
                s.sendall(b'SIGNAL NEWNYM\r\n'.encode('utf-8'))
                response = s.recv(1024)
                if b"250" in response:
                    print("[Tor Success] Circuito Tor rotado correctamente (Nueva IP asignada).")
                else:
                    print(f"[Tor Warning] Respuesta inesperada al rotar circuito: {response}")
        except Exception as e:
            print(f"[Tor Control Error] No se pudo conectar al puerto de control de Tor: {e}")
