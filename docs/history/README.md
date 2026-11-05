# TaskForge r57

## Что изменилось

Primary -> Cloudflare DNS -> пауза 15 с -> три успешные HTTP-проверки публичного маршрута -> Certbot, если нужен -> HTTPS -> traffic_ready.

r57 — минимальный hotfix r56: явные команды `cloudflare wait` и `primary ... --timeout` завершаются только по текущему `edge_phase=ready`. Фоновая периодическая перепроверка может временно показывать `route-check 1/3`, не сбрасывая уже свежую доказанную готовность сайта; такая перепроверка больше не считается новым завершённым handoff.
`cloudflare wait` также сразу откажется работать, если на локальной ноде ещё не установлен stable r57, вместо многоминутного ожидания старого Agent.

Один архив на всех нодах: A/B full, C lite. Нет автовозврата на A. Миграция по фактическому состоянию из r55 сохранена; старые релизы не исполняются.

## Обновление

Если A сейчас Primary: B -> C -> A, по одной ноде. Из новой распакованной папки r57:

```bash
sudo bash ./cluster.sh migrate &&
bash ./cluster.sh status &&
bash ./cluster.sh doctor
```

При ошибке остановиться. Не удалять volumes, ключи и служебные файлы миграции. Ноды с уже корректными stable mounts идут по пути без re-home.

## Cloudflare API Token

Сначала обновить все три ноды. Затем Cloudflare: My Profile -> API Tokens -> Create Token -> Edit zone DNS / Use template.

Permissions:
- Zone / DNS / Edit
- Zone / Zone / Read

Zone Resources: Include / Specific zone / taskforge.by.

Нужен именно User API Token, не Global API Key. Токен не публиковать в чате и не добавлять в архив.

На B, затем C, затем A:

```bash
sudo bash ./cluster.sh cloudflare setup &&
sudo bash ./cluster.sh cloudflare preflight
```

Вставить токен в скрытый ввод. Токен может быть один на A/B/C. Setup сам сохранит его в `/etc/taskforge/cloudflare-api-token`, root, 0600. На standby DNS не переключается; на Primary агент подхватит настройку автоматически.

На текущей Primary A:

```bash
sudo bash ./cluster.sh cloudflare wait --timeout 600 &&
bash ./cluster.sh status
```

Нужны `edge_phase=ready`, `public_route_ready=True`, `traffic_ready=True`. `preflight` проверяет API/DNS, но не доказывает публичную готовность.

## Проверка переключения

Только после настройки всех нод и успеха `cloudflare wait`. В окно обслуживания:

```bash
sudo bash ./cluster.sh primary B --timeout 600
```

После проверки можно вернуть A такой же командой. Если ожидание истекло, БД уже могла переключиться: сначала смотреть `status`, не повторять switchover вслепую. Автоотката Primary нет.

## Границы проверки

Прогнаны 61 автоматический тест edge/readiness логики и регрессии миграции. HTTP/TLS проверены на локальных тестовых серверах. Боевые Docker, Cloudflare и Let's Encrypt в этой среде не запускались. Нужен контролируемый боевой тест после настройки токена.

Подробности: `R57_CHANGES.md`, `CLOUDFLARE_SETUP_R57.md`, `VALIDATION.md`.
