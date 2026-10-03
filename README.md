# ChamosaNews

Panel estático para GitHub Pages con:

- **Calendario económico** de la semana actual y la siguiente: hora de Madrid, divisa, impacto, dato real, consenso, anterior y cambio del consenso frente al dato anterior. Filtros por día, impacto y divisa.
- **Franja de hoy**: línea temporal con las sesiones de Tokio, Londres y Nueva York, cada publicación marcada según su impacto y la hora actual. Pulsar una marca lleva a su fila del calendario.
- **Indicadores clave** de EE. UU., eurozona y España, cada uno con su último dato, el anterior, el cambio, una minigráfica con la tendencia y su próxima (o última) publicación con el consenso:
  - EE. UU.: NFP, paro, salario por hora, peticiones de paro, IPC, IPC subyacente, PCE subyacente, PIB, ISM manufacturero y de servicios, Michigan y tipos de la Fed.
  - Eurozona: IPC armonizado, IPC subyacente, paro, PIB, PMI compuesto y tipo de depósito del BCE.
  - España: IPC armonizado y paro registrado.
  Las series históricas salen de FRED y del BCE sin necesidad de clave. Al pulsar una fila se abre su publicación en el calendario.
- **Cuenta atrás** hasta el próximo dato de alto impacto.
- **Sentimiento**: Miedo y Codicia de CNN (acciones) y de alternative.me (cripto), y un mapa de calor con variación a 1 día, 5 días y 1 mes, RSI 14, posición frente a MM50/MM200 y un sesgo técnico de −100 a +100 para oro, plata, WTI, EUR/USD, GBP/USD, USD/JPY, índice dólar, S&P 500, Nasdaq 100, DAX, IBEX 35, bono a 10 años, VIX y Bitcoin.
- **Acciones a vigilar**: unas 60 acciones del IBEX 35 y grandes de EE. UU. puntuadas de 0 a 100 por valoración (PER frente a su sector, PEG), analistas (precio objetivo y recomendación), calidad (ROE, crecimiento del beneficio), momento (tendencia y RSI) y tono de sus noticias de la última semana. Muestra los motivos de cada puntuación, la fecha de resultados y, al pulsar, sus ratios y titulares. Se recalcula cada 6 h. Es un filtro para decidir qué mirar, no una recomendación.
- **Rupturas de máximos históricos**: todas las acciones de EE. UU. (NYSE, NASDAQ y NYSE American, sin ETF, warrants ni preferentes, precio > 5 $ y negociado > 2 M$ diarios; unas 3.000) y unas 85 grandes europeas. Muestra las que cierran hoy en máximo histórico, las que lo hicieron en los últimos 10 días y las que están a menos de un 3 %, con máximo anterior, base, volumen relativo, negociado medio, extensión y gráfico del último año. Tiene su propio workflow (`ath-scan.yml`): se ejecuta tras la apertura y tras el cierre de Wall Street, y el domingo recalcula el historial completo.
- **Titulares** RSS (FXStreet, Investing.com, ForexLive, Expansión) etiquetados por activo y con un tono estimado por palabras clave.

Autor: Marcos Chamosa

## Página Cripto (crypto.html)

Accesible desde la pestaña *Cripto* de la cabecera. La sección principal son las **rupturas de máximos**, con dos modos: máximo histórico (entre las 1.000 mayores de CoinGecko) y máximo de 1 año (todos los pares contra USDT de Binance con liquidez). Muestra en tarjetas las que rompen hoy y en tabla las recientes y las cercanas, con máximo anterior, base, volumen relativo, RSI, gráfico y señales; se puede filtrar por volumen mínimo y ordenar. Además incluye panorama (capitalización, dominancia, Miedo y Codicia, stablecoins, funding de bitcoin), mapa del mercado por capitalización, tabla de las 100 primeras con RSI y sesgo técnico, rupturas de máximos históricos, derivados de Hyperliquid, sectores, titulares cripto y la agenda macro de EE. UU. La actualiza `update_crypto.py` con su propio workflow (`crypto.yml`) cada 30 minutos, todos los días. Fuentes sin clave: CoinGecko, alternative.me, DefiLlama, Hyperliquid y Binance; opcionalmente puedes añadir el secret `COINGECKO_API_KEY` (plan Demo gratuito) para tener más margen.

## Cómo funciona

GitHub Pages solo sirve ficheros estáticos, así que los datos los descarga una GitHub Action (`.github/workflows/update-data.yml`) que ejecuta `update_data.py` cada 30 minutos en días laborables y guarda el resultado en `data/*.json`. La página lee esos JSON y se refresca sola cada 5 minutos. No hace falta ninguna clave de API.

## Puesta en marcha

1. Crea un repositorio nuevo en GitHub (público, para que Actions y Pages sean gratis) y sube todo el contenido de esta carpeta, incluida la carpeta oculta `.github`.
2. En **Settings → Pages**, elige *Deploy from a branch*, rama `main` y carpeta `/ (root)`.
3. En **Settings → Actions → General → Workflow permissions**, marca *Read and write permissions* para que la Action pueda guardar los datos.
4. En la pestaña **Actions**, abre *Actualizar datos de mercado* y pulsa **Run workflow**. Después abre *Escanear máximos históricos* y pulsa **Run workflow** también. La primera pasada del escáner descarga el historial completo de unas 7.000 acciones y puede tardar entre 30 y 90 minutos; las siguientes son mucho más rápidas. A partir de ahí todo se ejecuta solo.
5. La página queda en `https://<tu-usuario>.github.io/<repositorio>/`.

Hasta la primera ejecución verás datos de ejemplo con un aviso arriba.

## Fuentes de datos y claves

| Parte de la página | Fuente principal | Respaldo |
|---|---|---|
| Calendario y consensos | Forex Factory | — |
| Miedo y Codicia | CNN, alternative.me | — |
| Indicadores macro | FRED, BCE | — |
| Titulares | RSS (FXStreet, Investing, ForexLive, Expansión) | — |
| Mapa de calor y acciones a vigilar | Yahoo Finance | Stooq (solo precios) |
| Máximos históricos de EE. UU. | Massive (antes Polygon.io), una llamada por sesión | Yahoo Finance |
| Historial largo (caché semanal) y Europa | Yahoo Finance | Stooq (Europa) |

Todo funciona sin claves, pero entonces Yahoo carga con todo. Con las claves gratuitas el escáner de EE. UU. pasa a hacer una sola petición por día de mercado y hay respaldo si Yahoo falla.

1. **Massive**: crea una cuenta gratuita en https://massive.com y copia la clave en https://massive.com/dashboard/keys (las claves antiguas de Polygon.io también valen).
2. **Stooq** (opcional): entra en https://stooq.com/q/d/?s=spy.us&get_apikey, resuelve el CAPTCHA y copia la clave.
3. En GitHub, ve a **Settings → Secrets and variables → Actions → New repository secret** y crea `MASSIVE_API_KEY` y, si la tienes, `STOOQ_API_KEY`.

Las claves nunca aparecen en el código ni en la web.

El escáner guarda las cotizaciones del último año de EE. UU. en la caché de GitHub Actions (carpeta `data_store/`, que no se sube al repositorio). Cada pasada solo pide a Massive los días que faltan, a razón de 5 llamadas por minuto. Si la caché se pierde (GitHub la borra tras 7 días sin uso) o faltan más de 25 días, se reconstruye con Yahoo automáticamente.

## Probar en local

```bash
pip install -r requirements.txt
export MASSIVE_API_KEY=tu_clave   # opcional
export STOOQ_API_KEY=tu_clave     # opcional
python update_data.py          # todo menos máximos
python update_data.py --ath    # escáner de máximos
python -m http.server 8000   # y abre http://localhost:8000
```

Abrir `index.html` con doble clic no funciona: el navegador bloquea la lectura de los JSON desde `file://`.

## Personalizar

- Activos del mapa de calor: lista `ASSETS` en `update_data.py` (tickers de Yahoo Finance).
- Máximos históricos: lista `EUROPE` para Europa; en EE. UU. el universo es automático y los filtros mínimos están en `US_MIN_PRICE` y `US_MIN_DOLLAR_VOL`.
- Acciones vigiladas: diccionario `STOCKS` en `update_data.py` (tickers de Yahoo; las españolas acaban en `.MC`). Los pesos de la puntuación están en `score_rows`.
- Indicadores clave: lista `INDICATORS` (código de serie de FRED o del BCE, transformación `level`, `diff`, `mom` o `yoy`, y la expresión `cal` que lo enlaza con el calendario).
- Fuentes de noticias: lista `FEEDS`. Etiquetas por activo: `ASSET_TAGS`.
- Frecuencia: las líneas `cron` del workflow (hora UTC). GitHub puede retrasar las ejecuciones programadas unos minutos.
- Traducciones de los eventos del calendario: lista `ES` en `index.html`.

## Limitaciones

- El feed de Forex Factory trae consenso y anterior; el dato real no siempre aparece en él. Cuando viene, la columna *Real* se colorea según quede por encima o por debajo del consenso.
- El tono de los titulares es una heurística por palabras clave, no un análisis semántico.
- El sesgo técnico es orientativo. Nada de este panel es asesoramiento financiero.
