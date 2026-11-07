# Клиентская сборка Blockfield

Моды, конфигурации и ресурсы клиента Blockfield для Minecraft 1.21.1 / Fabric. Готовые дистрибутивы публикуются в [blockfield-releases](https://github.com/Blockfield/blockfield-releases).

История переписана 2026-09-27: схемы WorldEdit удалены из всех коммитов (теперь они в приватном архиве `Blockfield/blockfield-schematics`), тег `v2.54.1` перенесён на отфильтрованный коммит. После сохранения локальных изменений старый клон можно обновить: `git fetch --force --tags --prune --prune-tags origin && git reset --hard origin/main` (или клонировать заново). Опубликованный `v2.54.1` остаётся в релизах и образах; побайтное восстановление из резервной копии проверено и описано в [HISTORY-RECOVERY.md](docs/HISTORY-RECOVERY.md).

```sh
python3 -m unittest discover -s tests -v
just build --out dist
```

Нужны Python 3.12+ и авторизованный `gh` для приватных зависимостей. Шейдеры скачиваются с сайтов издателей по ссылкам и хешам из `shaderpacks/*.pw.toml`; ZIP-файлы в Git не хранятся. Моды закреплены URL и хешами в `mods/*.pw.toml`, архивы ганпака и схем WorldEdit — в `resources.lock.json`. JAR скачиваются из отдельных репозиториев; ресурсы R2 — по публичному HTTPS без ключей. В Git модпака бинарники не лежат.

Blacksmith проверяет main и PR. Тег `vX.Y.Z` должен совпадать с `pack.toml`: он выпускает клиентские архивы и `Blockfield-web.tar` (дерево раздачи `dist/web`, детерминированный tar; его SHA256 есть в `SHA256SUMS` релиза). Образ раздачи больше не собирается: nginx живёт в [blockfield-distribution](https://github.com/Blockfield/blockfield-distribution) и раздаёт пак с хоста (`<pack-dir>`). Deploy client сверяет совместимость с фактически готовыми Game и стабильным редактором Workshop на одном образе и порте; тест, переход или ошибка запрещают деплой. Затем шаг раздачи Deploy server скачивает `Blockfield-web.tar` из blockfield-releases, проверяет SHA256 и `build.json`, распаковывает в `releases/<tag>` и атомарно переставляет симлинк `current`; публичная проверка (`check-deployed.py`) и откат на прежний релиз занимают секунды, без rollout swarm. На хосте хранятся пять последних релизов. После этого разрешённый клиентский digest обновляется в обоих приложениях без перезапуска; частичная ошибка или рестарт восстанавливают оба gate. Latest публичного releases меняется только после успешного деплоя. Секреты Dokploy для раздачи не нужны. Схема и восстановление — `docs/DEPLOY.md` в blockfield-server. При смене контракта пак переключает Deploy server с `client_tag` после READY обоих приложений (порядок — в README сервера); Deploy client после него только проверяет пару, оба gate и ставит Latest.

### Зеркала

Файлы пака раздаются и с зеркал из `mirrors.json`: Gcore CDN тянет их с `blockfield.pro` по тем же путям,
а Cloudflare R2 (`r2.blockfield.pro`, бакет `blockfield-pack`, lifecycle удаляет
объекты через 30 дней после загрузки) и релиз `files` в
[blockfield-pack-mirror](https://github.com/Blockfield/blockfield-pack-mirror) хранят их под хешем, который
объявляет пак. CI тега выгружает недостающие файлы до деплоя (`scripts/mirror-files.py`; на GitHub до 400
за запуск, R2 — секреты `R2_PACK_ACCESS_KEY_ID`/`R2_PACK_SECRET_ACCESS_KEY` только на этот бакет), публикует
`mirror-manifest.txt` (ключи зеркал релиза) и обновляет объекты пяти последних релизов, чтобы откат всегда
нашёл их на зеркалах: R2 — server-side touch (перезапуск 30-дневного счётчика), GitHub — запрет удаления
(`retain` в `mirror-files.py`; старые релизы без манифеста доезжают по мере выхода новых). Если список
релизов или чужой манифест прочитать не удалось, в этом запуске ничего не удаляется, а текущий релиз
всё равно публикуется. Выгрузка
не блокирует выпуск: файла нет на зеркале — packwiz-installer берёт его из следующего источника. Сборка пишет
`mirrors.json` в корень раздачи; installer сам выбирает самое быстрое зеркало у каждого игрока, а
`blockfield.pro` остаётся последним источником ([подробности](tools/packwiz-installer/README.md)). Старые
инстансы Prism получают зеркала после повторного импорта `Blockfield-prism.zip`.

## Подключение

Игрок подключается по RakNet: `raknet;play.blockfield.pro:25566` (Raknetify, UDP),
единственный адрес в `servers.dat`. TCP 25565 обслуживает status ping и принимает вход
через тот же Drasl-шлюз для сетей, где режут UDP, см. [proxy README](<private-repo>#транспорт-клиента).
Raknetify закреплён сборкой `bf3` того же релиза, что у proxy; `tests/test_raknetify_pin.py`
проверяет версию и sha512.
Raknetify — клиентский мод; backend Fabric не требует его при подключении
через Velocity. UDP 25565 остаётся голосовым чатом. `servers.dat` помечен `preserve`:
у существующих установок старая запись «Blockfield (TCP)» остаётся и снова пускает по TCP.

`launcher.json` передаёт лаунчеру тот же `raknet;` адрес: лаунчер с 1.0.11 пингует
статус по TCP 25565 того же хоста, а адрес quick-play отдаёт игре без изменений.

Raknetify all-in-one — GPLv3; [исходники](https://github.com/RelativityMC/raknetify)
и license/notices стороннего JAR сохраняются при распространении.

## Совместимость

Контракт — SHA256 общих модов (`side = "both"`), ресурсов ганпака, Minecraft/Fabric и `protocol` из `compatibility.json`. Клиентские моды, пользовательские конфиги и версия сборки на него не влияют. При несовместимом изменении сетевых сообщений Blockfield необходимо поднять `protocol` на обеих сторонах; хеш общих модов не заменяет это решение разработчика. Изменения общих модов требуют согласованного выпуска клиента и сервера.

Для уже работающего сервера монолита есть узкое исключение по точному `server_runtime` и контракту 2.54.0. Новые серверы публикуют контракт сами. Пины клиента и сервера независимы; исходники друг друга не скачиваются.

Исходники [ганпака](https://github.com/Blockfield/gucci-vuitton-attachment), [схем](https://github.com/Blockfield/blockfield-schematics) и [музыки](https://github.com/Blockfield/blockfield-music) приватны; опубликованные входы сборок доступны через https://assets.blockfield.pro. [Порядок чтения и публикации](https://github.com/Blockfield/blockfield-workspace/blob/main/docs/ASSETS.md). Перенос не меняет права сторонних авторов.

## Проверки разработки

Для Linux и Windows нужны Python 3.12+, Node.js 22 и Just 1.57.0.
Инструменты проверок устанавливаются в кеш проекта.

```sh
just setup
just check
just format
```

Версионированные хуки (`.githooks` через `core.hooksPath`): pre-commit форматирует
staged-файлы теми же закреплёнными форматтерами и пере-stage'ит их, поэтому коммит
всегда отформатирован; pre-push запускает `just lint` и `just format-check` без Gradle
(обход только через `--no-verify`). Хуки включаются автоматически при любом
`just`-вызове: свежий клон получает хуки на первом `just`.
Если git уже берёт хуки из другого места (`core.hooksPath` в любом конфиге или
активные хуки в `.git/hooks`), `just` их не трогает и только предупреждает.
Если записать `core.hooksPath` не удалось, `just setup` завершается ошибкой.
`just --list` показывает команды сборки и разработки.

Условия использования: [LICENSE](LICENSE).
