# ⚡ SEARVGA NEXTGEN // Buscador Private T-800

![Docker Production](https://img.shields.io/badge/Docker-Production-2496ED?style=flat-square&logo=docker&logoColor=white)
![PowerShell 5.1+](https://img.shields.io/badge/PowerShell-5.1%2B-5391FE?style=flat-square&logo=powershell&logoColor=white)
![SearXNG Core](https://img.shields.io/badge/SearXNG-NextGen-00C7B7?style=flat-square&logo=searxng&logoColor=white)
![Tor SOCKS5](https://img.shields.io/badge/Tor-SOCKS5-7D4698?style=flat-square&logo=tor&logoColor=white)
![Nginx UI](https://img.shields.io/badge/Nginx-UI-009639?style=flat-square&logo=nginx&logoColor=white)
![License AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg?style=flat-square)

---

## 🌟 Resumen Ejecutivo

**SEARVGA NEXTGEN (Buscador Private T-800)** es un metamotor de búsqueda privado de alto rendimiento, diseñado para garantizar un anonimato estricto, cero telemetría y una experiencia visual inmersiva con la interfaz **T-800 Minimalista**. Combina la robustez de SearXNG con contenedores Docker optimizados, un proxy Tor integrado y un sistema de automatización en PowerShell de 1 clic.

---

## 🎨 Interfaz UX/UI: T-800 Minimalista

Desarrollada para ofrecer el máximo impacto visual sin sacrificar legibilidad:
- **Estética Cibernética Avanzada**: Fondo oscuro plano de alto contraste (`#010a04`) con detalles neón en verde Matrix (`#00ff66`) y rojo T-800 (`#ff2d20`).
- **HUD Ocular y Marcos Angulares**: Elementos visuales dinámicos que emulan un visor de combate cibernético.
- **Telemetría en Tiempo Real**: Barra inferior persistente con estado del enlace (`EN LINEA`), latencia exacta en milisegundos y recuento de resultados.
- **Etiquetas por Motor**: Identificación visual instantánea de la fuente de cada resultado.

---

## 🛡️ Características Principales

- **Privacidad Absoluta**: Sin cookies de seguimiento, sin almacenamiento de historiales personales y sin perfilado de usuario.
- **Tri-Modo de Red Inteligente**:
  - **Directo**: Conexión nativa de velocidad máxima.
  - **Híbrido**: Enrutamiento selectivo a través de pasarelas anónimas para motores compatibles.
  - **Tor**: Anonimización estricta mediante proxy SOCKS5 (`socks5h://tor:9150`).
- **Despliegue Automatizado (1 Clic)**: Scripts inteligentes en PowerShell con autoreparación, validación de salud (`/healthz`) y gestión de puertos.

---

## 🏗️ Arquitectura del Clúster Docker

```
┌────────────────────────────────────────────────────────┐
│                        Cliente                         │
│                    (http://localhost:8890)             │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│               searvga_ui (Nginx Frontend)              │
│       - Interfaz Web T-800 Minimalista / SPA           │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│               searvga_core (SearXNG Core)              │
│       - Granian ASGI + Motores Agregados               │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│              searvga_tor (Tor SOCKS5 Proxy)            │
│       - Encriptación y Anonimato de Tráfico            │
└────────────────────────────────────────────────────────┘
```

---

## 🚀 Guía de Instalación y Despliegue Rápido

### 1. Requisitos Previos
- Windows 10 / 11 con PowerShell 5.1 o superior.
- Docker Desktop instalado y en ejecución con soporte para Docker Compose v2+.

### 2. Clonar el Repositorio
```bash
git clone https://github.com/martillohamer/searxng-nextgen.git
cd searxng-nextgen
```

### 3. Despliegue en 1 Clic (PowerShell)
Ejecute el script maestro de inicio:
```powershell
powershell -ExecutionPolicy Bypass -File ".\Buscador Private T-800.ps1"
```

### 4. Acceso al Sistema
- **Interfaz T-800 Minimalista**: [http://localhost:8890](http://localhost:8890)
- **Motor SearXNG Directo**: [http://localhost:8889](http://localhost:8889)

---

## 🔀 Modos de Red Operativos

| Modo | Comando / Parámetro | Descripción |
|---|---|---|
| **Directo** | `-Modo Directo` | Rendimiento máximo sin intermediarios de red. |
| **Híbrido** | `-Modo Hibrido` | Balance inteligente entre velocidad y privacidad proxy. |
| **Tor** | `-Modo Tor` | Enrutamiento cifrado a través de la red de nodos Tor. |

---

## 🛠️ Mantenimiento y Sincronización

Para sincronizar actualizaciones con el repositorio remoto de GitHub:
```powershell
powershell -ExecutionPolicy Bypass -File ".\sincronizar-github.ps1"
```

---

## 📄 Licencia
Este proyecto se distribuye bajo la licencia **GNU General Public License v3.0 (AGPL-3.0 / GPLv3)**. Consulta el archivo `LICENSE` para más detalles.
