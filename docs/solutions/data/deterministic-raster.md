# Повторяемая растеризация

## Context
Одинаковый recipe должен воспроизводить reference, current и разметку.

## Symptoms
После мутации менялись 4–13 пикселей скруглённых границ, на один уровень канала. DOM оставался одинаковым.

## What did not work
Два последовательных одинаковых screenshot не гарантировали повторяемость следующего запуска. Удаление disclosure marker не устранило расхождение.

## Root cause
Частичная растеризация Chromium меняла сглаживание границ после перерисовки.

## Solution
Worker запускает Chromium с `--disable-partial-raster`.

## Why it works
Изменённые tiles растеризуются целиком. Flag описан в [chrome-launcher](https://github.com/GoogleChrome/chrome-launcher/blob/main/docs/chrome-flags-for-tools.md#rendering--gpu).

## Verification
Regression через worker сравнивает PNG и labels в 20 повторах. До исправления получались разные hashes; после исправления тест проходит.

## Prevention
При обновлении Chromium проверять повторяемость изменённых сцен и результаты разных чисел workers.

## Applies when
Статические DOM-сцены в закреплённом окружении Chromium. Проверка не доказывает межплатформенную идентичность.
