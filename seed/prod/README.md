# Production seed (`APP_ENV=prod`) — empty for now

Put the production rows here, one CSV per table, in the same format as `seed/dev/`
(no `id` column; parents named by key — see `seed/README.md`):

    user.csv  cex_account.csv  subaccount.csv  strategy.csv  strategy_rule.csv

`make seed` with `APP_ENV=prod` loads `seed/common/` and then this folder. Until CSVs are added
here, production gets only the common rows (contract sizes, Telegram notification settings).

These files hold real e-mail addresses and account labels: keep them out of a public repository.
