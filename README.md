# AutostopManager

Headless long-term memory for the AutoStop CRM manager agent.

This project does not replace AutoStop CRM and does not duplicate CRM cards,
clients, vehicles, repair orders, or cashbox data. CRM remains the source of
truth. AutostopManager stores only manager memory: facts, notes, tasks,
reminders, journal entries, and operating rules that should survive between
Codex sessions and ChatGPT mobile usage.

## First Version

- storage: SQLite at `data/autostop_manager.sqlite3`
- local access: `python -m autostop_manager.cli ...`
- MCP access: `python -m autostop_manager.mcp_server`
- docs for agents: `docs/agent/`

## CLI Examples

```powershell
python -m autostop_manager.cli remember "Аренда бокса оплачивается до 5 числа" --kind fact --tags аренда
python -m autostop_manager.cli recall аренда
python -m autostop_manager.cli task "Проверить просроченные машины утром" --due 2026-04-30
python -m autostop_manager.cli remind "Напомнить про аренду" --due 2026-05-04T10:00:00+07:00
python -m autostop_manager.cli today
python -m autostop_manager.cli journal "Проверил доску CRM, готовые машины требуют оплаты"
```

## MCP Tools

The manager memory tools are intentionally separate from CRM operations:

- `remember`
- `recall`
- `add_manager_task`
- `today_context`
- `manager_journal`

AutoStop CRM operations still use the existing AutoStop CRM MCP tools such as
`bootstrap_context`, `get_board_context`, `review_board`, `search_cards`,
`get_card_context`, and `list_repair_orders`.
