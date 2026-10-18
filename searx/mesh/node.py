import hmac
import hashlib
import json
import socket
import threading

class MeshNode:
    def __init__(self, host: str, port: int, secret_key: str):
        self.host = host
        self.port = port
        self.secret_key = secret_key.encode('utf-8')
        self.peers = []  # Lista de tuplas (host, port) de nodos conocidos
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((self.host, self.port))
        self.is_running = False

    def _sign_message(self, message: bytes) -> str:
        """Genera una firma HMAC-SHA256 para garantizar la integridad del mensaje."""
        return hmac.new(self.secret_key, message, hashlib.sha256).hexdigest()

    def send_secure_message(self, target_host: str, target_port: int, payload: dict):
        """Envía un paquete cifrado y firmado HMAC a otro nodo de la malla."""
        data_str = json.dumps(payload)
        data_bytes = data_str.encode('utf-8')
        signature = self._sign_message(data_bytes)
        
        packet = {
            "signature": signature,
            "payload": payload
        }
        
        packet_bytes = json.dumps(packet).encode('utf-8')
        try:
            self.socket.sendto(packet_bytes, (target_host, target_port))
        except Exception as e:
            print(f"[Mesh Error] No se pudo enviar mensaje a {target_host}:{target_port} -> {e}")

    def start_listener(self):
        """Inicia el hilo de escucha pasiva para recibir paquetes de la red mesh."""
        self.is_running = True
        def listen():
            while self.is_running:
                try:
                    packet_bytes, addr = self.socket.recvfrom(4096)
                    packet = json.loads(packet_bytes.decode('utf-8'))
                    
                    received_sig = packet.get("signature")
                    payload = packet.get("payload")
                    
                    # Validar integridad con HMAC
                    data_bytes = json.dumps(payload).encode('utf-8')
                    expected_sig = self._sign_message(data_bytes)
                    
                    if hmac.compare_digest(received_sig, expected_sig):
                        self.handle_valid_packet(payload, addr)
                    else:
                        print(f"[Security Warning] Paquete con firma HMAC inválida desde {addr}")
                except Exception as e:
                    if self.is_running:
                        print(f"[Mesh Listener Error] {e}")

        threading.Thread(target=listen, daemon=True).start()
        print(f"[Mesh Node] Nodo iniciado y escuchando en {self.host}:{self.port}")

    def handle_valid_packet(self, payload: dict, addr: tuple):
        """Procesa peticiones legítimas de otros nodos (búsquedas compartidas, heartbeats)."""
        action = payload.get("action")
        print(f"[Mesh Packet] Acción recibida '{action}' desde el nodo {addr}")
        
        if action == "HEARTBEAT":
            pass
        elif action == "QUERY_RELAY":
            pass

    def stop(self):
        self.is_running = False
        self.socket.close()
