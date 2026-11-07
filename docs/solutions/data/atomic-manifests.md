# Атомарная проекция manifest

## Context
SQLite хранит checkpoint импорта; JSONL — его производная проекция.

## Symptoms
Ошибка записи manifest оставляла временный файл и открытые ресурсы.

## What did not work
Последовательный cleanup после записи без `finally`.

## Root cause
Исключение в проекции пропускало закрытие DB и lock.

## Solution
Проверять бюджет при проекции, заменять manifest после flush/fsync, закрывать ресурсы независимо от результата cleanup.

Именованные sidecar-файлы pending-сцены заменять атомарно до checkpoint. Не сохранять старую ссылку только потому, что файл существует.

## Why it works
Незавершённая проекция не заменяет предыдущий manifest. Checkpoint остаётся доступен для resume.

## Verification
Regression прерывает частично записанный manifest, проверяет предыдущий файл и повторный resume.

Capture regression прерывает запись после sidecar, до checkpoint. После resume содержимое именованного файла совпадает с CAS digest в manifest.

## Prevention
Проверять отдельно импорт и запись производных файлов.

## Applies when
Manifest восстанавливается из устойчивого checkpoint.
