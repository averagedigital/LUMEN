# Проверка видимого текста

## Context
Разметка браузерных мутаций текста.

## Symptoms
Обрезанный baseline принимался как clean; невидимая цель получала bbox.

## What did not work
Общий pixel diff, локальный opacity и обычный hit-test.

## Root cause
Opacity родителя не наследуется. `pointer-events:none` исключает видимый слой из hit-test. Частичный cutoff исключался из baseline-проверки.

## Solution
Учитывать opacity предков, проверять горизонтальный cutoff и включать слои в hit-test на время измерения.

## Why it works
Геометрия проверяется только для видимых целей; перекрывающие слои участвуют в проверке.

## Verification
Три browser regression-теста воспроизводят исходные ошибки и проходят после исправления.

## Prevention
Новые эффекты требуют отдельных positive, negative и no-op fixtures.

## Applies when
Статические DOM-сцены с непрозрачным текстом. Это не универсальная проверка canvas, filters и всех CSS paint effects.
