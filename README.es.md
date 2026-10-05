# RoboControl

**[Read in English →](README.md)**

RoboControl convierte una Raspberry Pi (o una Jetson Nano) y una placa RaspiRobot Board V3 en un pequeño robot que se maneja desde el navegador. Apuntás el teléfono a la IP del robot y tenés video en vivo, un control direccional, cámara con pan-tilt y telemetría básica — sin instalar ninguna app.

Por dentro es una aplicación FastAPI con una capa de hardware abstraída, así que el mismo código corre el robot real en una Pi o una Jetson, y una versión simulada en Docker cuando solo querés probar el tablero sin hardware. Es un proyecto personal, no un producto comercial — y esta página intenta reflejar eso: qué hace de verdad, qué todavía no hace, y qué conviene revisar antes de confiar en él con hardware real.

Para la documentación técnica completa — instalación paso a paso, referencia de la API, configuración, seguridad, monitoreo — el README en inglés (enlace arriba) es la fuente completa; esta página es la introducción en español.

---

## En palabras simples

Si no programás, esto es lo que es en realidad: un robot chico — motores, ruedas, una cámara, un sensor de distancia — que se maneja desde una página web, como usarías cualquier app. Nada que instalar. Abrís el navegador, ves lo que ve el robot, y lo manejás.

Lo interesante no es el robot en sí — kits de robótica hay muchos. Es *por qué* está construido así. Un rover de la NASA en Marte no se puede manejar con un joystick en tiempo real: una señal de radio tarda entre 3 y 22 minutos en cruzar la distancia entre la Tierra y Marte, solo de ida, sin importar qué tan buena sea la tecnología — es la velocidad de la luz, no una limitación de ingeniería. Por eso el rover recibe un solo lote de comandos por día, los ejecuta casi solo, y tiene que decidir por su cuenta qué es un obstáculo y qué no. Este robot, en la misma red WiFi que el teléfono que lo maneja, tiene una latencia de bastante menos de un segundo — pero igual tiene que responder una versión más chica de la misma pregunta que enfrenta cualquier sistema remoto real: *¿qué hace cuando no te puede consultar?* Si el sensor detecta un obstáculo, se frena solo antes de que un humano pueda reaccionar. Si no encuentra una red WiFi conocida, crea la suya propia y pide ayuda en vez de quedarse desconectado. Escala distinta, misma idea de fondo.

No hace falta el presupuesto de la NASA para empezar a pensar así — hace falta unos cien dólares en piezas y un fin de semana libre. Ese es, en el fondo, el punto de este proyecto: las ideas detrás de los sistemas remotos serios se pueden aprender, no son exclusivas de nadie, y una Raspberry Pi es un buen lugar para empezar a practicarlas.

Si querés verlo funcionando antes de seguir leyendo, mirá las capturas de pantalla en el README en inglés, sección **4. Using the Controller**, o probá el tablero sin ningún hardware con [`docker/README.md`](docker/README.md) — `docker compose -f docker/docker-compose.yml up --build` y listo.

---

## Lo esencial, rápido

| | |
| --- | --- |
| **Qué hardware necesita** | Raspberry Pi 3B+/4/5 o Jetson Nano, una placa RaspiRobot V3, un sensor ultrasónico HC-SR04, dos motores — menos de $100 en total |
| **Cómo se instala** | Clonar el repo, `bash scripts/install.sh`, abrir el navegador — ver Sección 2 del README en inglés |
| **Cómo se conecta a WiFi** | Un archivo `wifi.txt` en la tarjeta SD, o un portal cautivo propio si no tenés forma de configurarlo — ver Sección 3 |
| **Licencia** | MIT — lo pueden clonar, modificar y usar libremente |
| **Repositorio** | [github.com/ZioGuillo/robocar](https://github.com/ZioGuillo/robocar) |

---

## Para estudiantes y curiosos

Si esto lo estás viendo después de una charla o clase: el código es real, no un tutorial de ejemplo — tiene pruebas automatizadas, documentación, y corre en producción hoy. Es MIT, así que un profesor lo puede tomar y usarlo en un curso sin pedirle permiso a nadie, y un estudiante lo puede clonar y tenerlo andando en una tarde.

No hace falta empezar por el robot físico. `docker/README.md` explica cómo correr el tablero completo — login, control de motores, cámara simulada, telemetría — en una laptop, sin ningún hardware. Es la forma más rápida de entender cómo está armado antes de gastar en piezas.
