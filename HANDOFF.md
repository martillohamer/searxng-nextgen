# HANDOFF — searxng-nextgen (Buscador Private T-800)

- **Fecha:** 2026-10-19
- **Proyecto:** searxng-nextgen / SearVGA NextGen
- **Fase:** D — Rediseño UI "T-800 Minimalista" & Integración Frontend Nginx
- **Estado:** 100% OPERATIVO — Clúster Docker completo (`searvga_core`, `searvga_tor`, `searvga_ui`) en puertos `8889` y `8890`.

---

## 🏛️ Registro de Decisiones de Arquitectura (ADR)

### ADR-04: Rediseño Visual "T-800 Minimalista"
- **Contexto:** Las iteraciones anteriores incluían animaciones pesadas de lluvia de caracteres estilo Matrix (*digital rain* en canvas completo) que, si bien resultaban llamativas, sobrecargaban visualmente la pantalla y dificultaban la lectura fluida de los resúmenes y títulos de los resultados de búsqueda.
- **Decisión:** Evolucionar hacia un diseño propio, moderno, nítido y reluciente denominado **T-800 Minimalista**. Se eliminaron los elementos flotantes distractores y se estableció un fondo oscuro plano de alto contraste (`#010a04`) con acentos neón personalizables (verde Matrix `#00ff66` / rojo T-800 `#ff2d20`), HUD ocular animado minimalista, marco con esquinas angulares cibernéticas y barra de telemetría inferior en tiempo real.
- **Resultado:** Apariencia cibernética de gran impacto visual y elegancia técnica, combinada con una legibilidad perfecta y tiempos de respuesta instantáneos en `http://localhost:8890`.

---

## 📋 Resumen del Clúster Actual

1. **Frontend (`searvga_ui`):** Servido por Nginx en el puerto `8890`, ejecutando la SPA optimizada con interfaz T-800 Minimalista.
2. **Backend (`searvga_core`):** SearXNG + Granian ASGI ejecutándose en el puerto `8889` con clave secreta de seguridad configurada en `searx/settings.yml`.
3. **Red SOCKS5 (`searvga_tor`):** Contenedor Tor para anonimización y soporte opcional de navegación profunda.

---

## 🛠️ Comandos de Mantenimiento

- **Reiniciar el clúster completo:**
  ```powershell
  docker compose -f docker-compose.prod.yml -f docker-compose.ui.yml restart
  ```
- **Ver logs de servicios:**
  ```powershell
  docker compose -f docker-compose.prod.yml -f docker-compose.ui.yml logs -f
  ```
