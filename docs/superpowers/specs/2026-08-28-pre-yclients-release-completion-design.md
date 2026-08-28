# Pre-YCLIENTS Release Completion Design

## Цель

Довести текущий локальный Telegram Production V1 до одного проверенного commit-pinned кандидата на staging, провести полную независимую ручную приёмку и подтвердить безопасный откат, не возобновляя YCLIENTS и не выполняя production rollout.

## Исходное состояние

- Staging работает на проверенном commit `220d03e5880f3645586c63090766671a3e8e9eaa`, image tag `rc-220d03e5880f3645586c63090766671a3e8e9eaa`, schema `0012_projection_suppression`.
- Локальный `main` находится на commit `26eb473df2547f224768bdc2892afe85990e114d`, schema head `0017_llm_compact` и содержит завершённые Router, Input Security, Validator и локальную реализацию Compact Context.
- Compact Context остаётся открытым только до платного real-provider прогона immutable suite из `40` синтетических кейсов и независимой SQL-сверки агрегатов.
- Ручная приёмка старого staging не является доказательством для текущего локального кандидата.

## Границы

В scope входят:

- Compact real-provider acceptance после отдельного разрешения владельца;
- свежие Docker, migration, privacy/static и review gates на точном итоговом commit;
- подготовка и commit-pinned развёртывание нового staging-кандидата после отдельного разрешения владельца;
- полная ручная приёмка Telegram и админки без реальных ПД;
- исправление только подтверждённых дефектов с повтором затронутых проверок;
- финальные staging smoke, safe-log scan и image-only rollback rehearsal.

Вне scope остаются:

- любые YCLIENTS read/write действия и изменение прав приложения;
- production rollout;
- создание аккаунта заказчицы и передача TOTP/секретов;
- push в GitHub без отдельного запроса;
- VK, Instagram, WhatsApp, cross-channel profile и кампании.

## Последовательность

1. Получить отдельное разрешение владельца и выполнить ровно один полный Compact Evaluation на настроенном `gpt-4.1-mini`. Проверить `40/40` dataset coverage, `100%` critical, не менее `95%` total, `0` errors и независимо сверить SQL-агрегаты.
2. При провале не повторять платный suite вслепую: локализовать причину, исправить test-first, прогнать затронутые локальные проверки и запросить разрешение на новый полный внешний прогон.
3. После зелёного Compact gate выполнить свежий полный Docker gate, migration cycle до единственной head `0017_llm_compact`, статические/privacy-проверки и итоговый review на точном candidate commit.
4. После отдельного разрешения развернуть этот commit-pinned кандидат на staging без YCLIENTS-мутаций и подтвердить healthchecks, schema и совпадение image IDs.
5. Провести полный human QA из `docs/qa/manual/Ручное тестирование человеком.md` через Telegram и админку, сохранить доказательства в корневом `tmp/`, проверить свежие логи и оставить бота снятым с паузы.
6. Исправлять только воспроизведённые дефекты. Каждый fix проходит RED → GREEN, затронутый regression gate, commit и повторный staging rollout.
7. На чистом кандидате выполнить финальный staging smoke, safe-log scan и `candidate → previous → candidate` rollback rehearsal без DB downgrade.

## Ворота владельца

Нужно отдельное явное подтверждение перед:

- платным Compact real-provider acceptance;
- изменением работающего staging;
- любым повторным платным прогоном после исправления;
- любым выходом за границы этого design, включая YCLIENTS, production и push.

## Критерий завершения

Pre-YCLIENTS release completion закрыт, когда Compact Evaluation прошёл, точный кандидат имеет свежие зелёные локальные gates и review, этот же кандидат развёрнут и полностью принят на staging, подтверждены чистые логи и rollback → restore, а все найденные блокирующие дефекты закрыты и перепроверены.
