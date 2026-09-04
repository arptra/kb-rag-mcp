# Known limitations

- Reflection, runtime DI selection, generated code вне checkout и service discovery без
  конфигурационного следа нельзя доказать статически.
- Динамически собранные URL/topic могут остаться с target hint без точного target.
- Lombok/KSP/compiler plugins не исполняются; анализ видит только сохранённый source.
- Межъязыковые вызовы за пределами Java/Kotlin требуют отдельного algorithm plugin.
- Совпадение имени `*Service` — кандидат, а не доказанная сетевая зависимость.
- GigaCode ограничен каталогом уже известных target services/entrypoints и не является
  источником истины без существующего `file:line`.
- Локальный checkout без Git commit нельзя replay-нуть бит-в-бит после изменения файлов;
  run явно показывает этот риск в repository record.
