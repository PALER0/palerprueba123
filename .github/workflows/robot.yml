name: Robot de anuncios

on:
  schedule:
    - cron: "*/10 * * * *"
  workflow_dispatch:
    inputs:
      probar:
        description: "Modo prueba: muestra lo que devuelve la web, sin avisar ni guardar"
        type: boolean
        default: false

concurrency:
  group: robot-anuncios
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  buscar:
    runs-on: ubuntu-latest
    timeout-minutes: 8
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - run: pip install -r requirements.txt

      - name: Preparar carpeta de estado
        run: mkdir -p state && touch state/.keep

      - name: Restaurar anuncios ya vistos
        uses: actions/cache/restore@v4
        with:
          path: state
          key: estado-${{ github.run_id }}
          restore-keys: estado-

      - name: Buscar anuncios
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          CONFIG_YAML: ${{ secrets.CONFIG_YAML }}
        run: python robot.py ${{ inputs.probar && '--probar' || '' }}

      - name: Guardar anuncios vistos
        if: ${{ !cancelled() && !inputs.probar }}
        uses: actions/cache/save@v4
        with:
          path: state
          key: estado-${{ github.run_id }}
