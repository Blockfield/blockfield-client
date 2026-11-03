# Восстановление сборок до очистки истории

Очистка 2026-09-27 сохранила код, но убрала бинарные входы из старых коммитов. Поэтому отфильтрованный тег не всегда достаточен для сборки старого релиза. Опубликованные архивы не менялись. Не переносите тег повторно и не возвращайте бинарники в текущую историю.

## Клиент v2.54.1 — проверенный путь

Полный бандл находится в рабочей папке владельца:
`.migration/backups/history-rewrite-2026-09-27/blockfield-client-before.bundle`.
SHA256: `55a86532a40f825dc752b29a2319c64e85c19ef6420bda2ca52b3c2c53731eba`.
Исходный коммит: `78df300b9e441d31e42fb4aa8519a5864858f25a`.

Из корня рабочей папки, используя новые, ещё не существующие каталоги:

```sh
sha256sum -c .migration/backups/history-rewrite-2026-09-27/blockfield-client-before.bundle.sha256
git clone .migration/backups/history-rewrite-2026-09-27/blockfield-client-before.bundle /tmp/bf-client-restore
git -C /tmp/bf-client-restore switch --detach v2.54.1
just python /tmp/bf-client-restore/scripts/build-client.py \
  --root /tmp/bf-client-restore --out /tmp/bf-client-restored-dist
gh release download v2.54.1 --repo Blockfield/blockfield-releases \
  --pattern SHA256SUMS --dir /tmp/bf-client-published-sums
cp /tmp/bf-client-restored-dist/web/build.json /tmp/bf-client-restored-dist/build.json
(cd /tmp/bf-client-restored-dist && sha256sum -c /tmp/bf-client-published-sums/SHA256SUMS)
```

Нужен авторизованный `gh`, читающий приватные зависимости. Проверено 2026-09-27: все четыре файла (`Blockfield-client.zip`, `Blockfield-prism.zip`, `Blockfield.mrpack`, `build.json`) совпали с релизом побайтно.

Среда упаковки существенна: проверенный Python 3.12.14 использовал стандартный zlib 1.3.2. Системный Python с zlib-ng создаёт другие сжатые ZIP при одинаковом содержимом. Совпадение игрового digest и побайтное совпадение ZIP — разные проверки.

## Исторические версии мода

После очистки `Blockfield/blockfield-mod` старые теги тоже могут ссылаться на деревья без `client/libs/*.jar`, хотя их Gradle-файлы ещё требуют эти библиотеки. Для таких тегов нужен `blockfield-mod-before.bundle` из той же папки резервных копий, затем checkout требуемого тега и его собственные инструкции сборки. SHA256 бандла: `9c563b0a2882e29230560b5bb890fb9de105711f06bfc12bd955786fb41fb6f7`.

Новые версии используют закреплённые внешние зависимости. Проверка исторических тегов на удалённые библиотеки и таблица переноса локальных веток сохранены рядом с бандлами в `../remaining-branches-2026-09-27/`. Все старые версии мода заново не собирались; для них не заявляется побайтная воспроизводимость.

## Проверка отката

`just python -m unittest discover -s tests -p test_rollback_http.py -v` выполняет настоящий HTTP-обмен с локальным имитатором Dokploy и раздачи. Некорректный кандидат не проходит штатную проверку опубликованных файлов; тест проверяет возврат на точный предыдущий legacy-образ и восстановление client gate.

Это интеграционная проверка API и файловых проверок. Docker, реальный Dokploy и игровой сервер в ней не запускаются; она не заменяет репетицию переключения контейнеров на отдельном staging-окружении.
