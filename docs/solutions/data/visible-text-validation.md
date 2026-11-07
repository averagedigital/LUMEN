# Проверка видимого текста

## Context
Разметка браузерных мутаций текста.

## Symptoms
Обрезанный baseline принимался как clean; невидимая цель получала bbox. Подписи для скринридеров ошибочно отклоняли baseline.

## What did not work
Общий pixel diff, локальный opacity и обычный hit-test.

## Root cause
Opacity родителя не наследуется. `pointer-events:none` исключает видимый слой из hit-test. Частичный cutoff исключался из baseline-проверки. Визуально скрытые accessibility-подписи сохраняют геометрию текста.

## Solution
Учитывать opacity предков, проверять горизонтальный cutoff и включать слои в hit-test на время измерения. Исключать стандартный скрывающий паттерн: absolute/fixed, не более 1×1 px, overflow hidden и нулевой clip либо inset(50%).

## Why it works
Геометрия проверяется только для видимых целей; перекрывающие слои участвуют в проверке.

## Verification
Browser regressions воспроизводят исходные ошибки. Отдельно проверены скрытый предок, обе формы clip, обычная обрезка и появление текста при фокусе. Accessibility-аннотации сохраняются.

## Prevention
Новые эффекты требуют отдельных positive, negative и no-op fixtures.

## Applies when
Статические DOM-сцены с непрозрачным текстом. Это не универсальная проверка canvas, filters и всех CSS paint effects.
