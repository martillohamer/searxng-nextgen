import hashlib
import time
import json

class MeshDistributedCache:
    def __init__(self, ttl_seconds: int = 3600):
        self.cache = {}
        self.ttl = ttl_seconds

    def _hash_key(self, query: str) -> str:
        return hashlib.sha256(query.strip().lower().encode('utf-8')).hexdigest()

    def get(self, query: str):
        key = self._hash_key(query)
        if key in self.cache:
            entry = self.cache[key]
            if time.time() - entry['timestamp'] < self.ttl:
                print(f"[Mesh Cache HIT] Resultados recuperados de la red para: '{query}'")
                return entry['data']
            else:
                del self.cache[key]
        return None

    def set(self, query: str, results: list):
        key = self._hash_key(query)
        self.cache[key] = {
            'timestamp': time.time(),
            'data': results
        }
        print(f"[Mesh Cache STORE] Resultados guardados en memoria Mesh para: '{query}'")
