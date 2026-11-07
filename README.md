# LUMEN

Layout Understanding & Monitoring Engine. Генератор проверяемых UI-дефектов и пар визуальных изменений.

Стек: TypeScript, Playwright, Python, OpenCV, SQLite.

Статус: пилот из 5 000 автоматически проверенных примеров. Основной корпус и ручной аудит не завершены. Обучение не запускается.

INSPECT: обрезка, перекрытие и недостаточный контраст текста, выход за viewport. DIFF: дефекты, clean-пары и корректные изменения состояния.

Экспорт: YOLO, COCO, DIFF pairs, локальный Hugging Face dataset. Изображения сохраняются без нарисованных bbox; raw change mask хранится отдельно.

COCO содержит общий `annotations.json` и отдельные `annotations-train.json`, `annotations-val.json`, `annotations-test.json` с исходным групповым разделением.

YOLO tiling: `--tile-size 1024 --tile-overlap 128`. Опция `--tile-uncovered original` сохраняет полный кадр, если tiles не покрывают все дефекты целиком. Manifest помечает его как `kind: original`; отчёт считает такие случаи отдельно от tiles. Без опции неполное покрытие останавливает экспорт.

Формат поддерживает необязательные версионированные аннотации элементов, состояний, взаимодействий и синтетических задач. Общая базовая сцена связывается с примерами через `scene_id`.

В конфиге генерации `workers` задаёт от 1 до 4 рендереров; по умолчанию 1. Результаты сохраняются в порядке recipes.

Внешние источники сохраняют provenance и условия использования. Непроверенные скриншоты и предсказания GUI-парсеров не считаются разметкой дефектов.

Подготовленные сцены могут включать локальные CSS, WOFF2, PNG, JPEG, WebP и SVG. Ресурсы проверяются по SHA-256 и переносятся в DIFF/HF-экспорты.

Модуль захвата публичных демо принимает конечные HTTPS-адреса из allowlist. Редиректы, неполные ресурсы и несовпадение офлайн-копии отклоняются.

`lumen-data sources capture --manifest candidates.jsonl --output captures --limit 100` сохраняет checkpoint и manifest для генератора. Повторный запуск обрабатывает только pending. Ошибки ресурсов останавливают запуск.

Строка candidates JSONL содержит `url`, `allowed_hosts`, `kind` (`screen` или `block`), `source_project_id`, `template_lineage_id`, `structural_family_id` и `provenance.license`. Захваты имеют статус `research_only`; точные дубликаты PNG исключаются.

Ход работы: [DEVLOG](docs/DEVLOG.md).
