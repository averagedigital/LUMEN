# LUMEN

Генератор синтетических UI-дефектов и пар скриншотов для анализа изменений интерфейса.

- INSPECT: изображение и координаты дефектов.
- DIFF: изображения до и после изменения, разметка и маска изменённых пикселей.
- Экспорт: YOLO, COCO, JSONL для Hugging Face Datasets.
- Дополнительные аннотации: элементы, состояния и взаимодействия.

TypeScript, Playwright, Python, OpenCV, SQLite.

```sh
docker build -t lumen-data .
docker run --rm lumen-data lumen-data --help
```

Команды CLI: `sources`, `generate`, `qa`, `review`, `export`, `reproduce`.
