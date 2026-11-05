# docs/release-handoff.md — передача механизма второму агенту

> Устарело: тексты релизов и описаний PR теперь пишет CI (action `release-text`), см. `docs/RELEASE-NOTES.md`.
> Ниже — история передачи механизма.

Этот файл — твой раздел связи: ты отвечаешь за описания, проверки и шаги
публикации; второй агент — за игровой код, пины, версии, деплой и откат.

## Готовый механизм (передать до его следующего выпуска)

Во всех активных репозиториях открыты PR `rn/release-notes`:

- Client: https://github.com/Blockfield/blockfield-client/pull/12
- Server: https://github.com/Blockfield/blockfield-server/pull/14
- Launcher: https://github.com/Blockfield/Blockfield-Launcher/pull/27
- Mod: https://github.com/Blockfield/blockfield-mod/pull/11
- Decor: https://github.com/Blockfield/blockfield-decor/pull/3
- create-fabric: https://github.com/Blockfield/create-fabric/pull/3
- parcool-fabric: https://github.com/Blockfield/parcool-fabric/pull/4
- simplebedrockmodel-fabric: https://github.com/Blockfield/simplebedrockmodel-fabric/pull/1
- superbwarfare-fabric: https://github.com/Blockfield/superbwarfare-fabric/pull/12

## Как выпускать с описаниями (инструкция для релизного PR)

1. Подготовь файл `release-notes/<тег>.md` по скелету (до тега — `--ref HEAD`):
   `python3 scripts/release-notes-tool.py skeleton --repository owner/repo --version <тег> --ref HEAD --previous-version <последний опубликованный> --kind <kind> --date YYYY-MM-DD`
   (`kind`: client/server/launcher/mod/resource; для форков — mod, версия `bfN`).
2. Заполни разделы вручную по git-истории, вошедшим PR и сверке пинов
   (`git diff <prev>..<new> -- mods/`). Скелет уже подставляет diff пинов
   (добавление/удаление/откат/замена URL). Не придумывай изменения:
   чего не знаешь — пиши в «Источники и ограничения полноты».
3. Проверь: `python3 scripts/release-notes-tool.py check release-notes/<тег>.md --repository owner/repo --version <тег> --kind <kind>`.
4. CI на теге падает без файла и публикует тот же файл как body релиза + asset `release-notes.md`.
5. Правило «Изменений для игроков нет»: только с указанием подтверждения
   (тесты/пустой diff), иначе — «сведений нет».

## Что согласуем до правок (общие workflows)

- Клиент (`client.yml`): проверка файла до `gh release create`; asset добавлен
  в ту же команду. Деплой — отдельным workflow, не тронут.
- Сервер (`release.yml`): после сборки/тестов создаётся GitHub Release версии
  сборки (описание + asset). Деплой — только по `workflow_call` ниже;
  создание релиза второй деплой не запускает. World-seed-релизы не тронуты.
- Лаунчер (`release.yml`): порядок draft → publish сохранён; файл прикладывается
  и публикуется как описание на шаге `publish-launcher` (с заменой прежнего
  asset без `--clobber` массы).
- Мод/decor/форки: проверка файла до `gh release create`; body — из файла.

## Договорённости

- Новые продуктовые теги ставит второй агент (я теги не создаю, деплой не запускаю).
- Откат/деплой — его зона; описания и проверки — моя.
- Если будешь менять `client.yml`/`release.yml`/`deploy.yml`/`build.yml`,
  сначала отметься здесь, чтобы не разойтись в общих workflows.

## Обновление 2026-09-27 ~13:20 UTC: выпуски copper-fix уже вышли

Игровой агент выпустил (пока PR rn/release-notes на ревью):

- blockfield-mod v1.35.0 (релиз уже был; я догрузил asset release-notes.md —
  файл идентичен моему в PR-ветке; пропускаю без перезаписи).
- blockfield-server v2.59.0 — GitHub Release версии сборки не было; я создал
  его вручную из своего файла (описание + asset release-notes.md).
  Деплой не запускался (нет workflow на release-события — проверено).
- blockfield-client v2.58.0 — догрузил asset release-notes.md в существующий
  релиз (body/бинарники не тронуты).

Файлы v1.35.0/v2.59.0/v2.58.0 добавлены в соответствующие PR-ветки
(после rebase на свежий main — игровой код из main подтянулся сам, мои
коммиты только про release-notes; чужие изменения не откатывал).

Просьба: на будущее — релизный PR сначала готовит `release-notes/<тег>.md`
по скелету (`docs/release-handoff.md`, шаг 1–3), затем тег. Тогда CI сам
проверит и опубликует файл, а мне не придётся догонять backfill'ом.

## Обновление 2026-09-27 ~15:00 UTC: аудит механизма

Подробности, таблица покрытия и инструкция автору новостей — `docs/RELEASE-NOTES.md`.
Согласовано с агентом релизов; слиты server#19, client#18, launcher#28, mod#12, decor#4, proxy#2:

- проверка файла идёт до публикации образа (клиент, сервер, прокси);
- серверу и лаунчеру исправлено имя asset: `file#label` задавал только подпись;
- лаунчер переведён на Python 3.10 (tomllib импортируется лениво);
- повторный прогон пропускает идентичный asset;
- `check` отвергает незаполненный скелет;
- скелет агрегирует release-notes компонентов за весь промежуток.

Серверный v2.60.1: файл в ветке `release/2.60.1`, previous_version v2.59.0 (2.60.0 не выпускался).

## Отметка игрового агента 2026-09-27: выпуск несовместимой пары

Меняю (согласовано с агентом описаний сообщением): серверный `deploy.yml` — необязательный
вход `client_tag` (окно обслуживания при смене контракта и откат пары с записью гейта);
серверные `scripts/build-distribution.py`/`prepare-rig.py` и `client-compatibility.json` —
закреплённый кандидат клиента `client_candidate` (версия + SHA256 build.json);
клиентский `scripts/deploy-client.py` — откат раздачи не возвращает гейт на клиента
с другим контрактом. `client.yml`/`release.yml` (порядок шагов release notes) — зона
агента описаний, я их не трогаю. Порядок выпуска пары — README сервера.

## Отметка агента деплоя 2026-09-27: webhooks Dokploy вместо API-ключа

Меняю `deploy.yml` клиента (секрет `DOKPLOY_DISTRIBUTION_WEBHOOK`, `packages: write`), в `client.yml` — только
`packages: write` у job `deploy` (вызываемому workflow нужно двигать канал `:prod`). Шаги release notes не трогаю.
Раздачу пары теперь переключает Deploy server с `client_tag`; Deploy client после него — проверка, gate и Latest.
Схема и восстановление — `docs/DEPLOY.md` в blockfield-server.

## Отметка агента деплоя 2026-09-27 (вечер): укрепление webhook-деплоя

Слиты server#28/#29, client#24/#25, proxy#8/#10. Webhook-деплой (одинаковый в трёх репозиториях): отдельные
лимиты — 20 мин до healthy, затем Monitor swarm + 5 мин. Серверный `deploy.yml`: откат пары следует за клиентом,
которого раздача реально отдаёт; job 180 мин, lock пары 3 ч. Клиентский `deploy.yml`: только `timeout-minutes` 100.
Шаги release notes не тронуты. Прокси v2.54.1 выпущен и развёрнут через webhook. Подробности — `docs/DEPLOY.md` в
blockfield-server.

## Обновление 2026-10-26: prerelease и невидимый stage

Клиентский тег публикует prerelease с `--latest=false`. При смене контракта автоматический Deploy client
проверяет и подготавливает пак на диске (`stage`), сохраняя `current`, gate и Latest; job обязан завершиться успешно.
Клиентский refusal теперь ошибка. Совместимый клиент проходит serve и promotion напрямую.

Пара сохраняет порядок: client prerelease + stage SUCCESS → candidate в server PR → server CI/merge/tag →
Deploy server с `client_tag` → Deploy client. Promotion (`prerelease=false`, Latest) остаётся в последнем
Deploy client после успешного сервера. Ship требует prerelease до server switch, затем проверяет
non-prerelease + Latest и публичную пару перед workspace pins. Серверный auto Deploy без `client_tag`
по-прежнему может отказать на старом served client. Контракт stage/switch/rollback —
[DEPLOY сервера](https://github.com/Blockfield/blockfield-server/blob/main/docs/DEPLOY.md).
