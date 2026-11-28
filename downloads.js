/* ============================================================
   ПОСИЛАННЯ НА ЗАВАНТАЖЕННЯ — єдине місце для редагування.
   ============================================================ */
window.DOWNLOADS = {
  /* 1) GitHub-репозиторій (формат «власник/репозиторій»).
        Сайт сам бере всі файли з його релізів і будує з них список версій.
        Залиште порожнім (''), якщо GitHub не потрібен. */
  repo: 'Technic45-creator/Asistent',

  /* 2) Файли вручну. Показуються завжди: поруч із файлами з GitHub,
        а якщо GitHub недоступний чи repo порожній — самі.
        Назва файлу має бути у форматі Імя-ВЕРСІЯ[-стадія][-збірка].zip
        (з неї сайт визначає версію). Додайте новий рядок для кожного файлу. */
  files: [
    {
      file: 'Asistent-2.6.1-alpha-1.zip',
      url:  'https://github.com/Technic45-creator/Asistent/releases/download/Asistent/Asistent-2.6.1-alpha-1.zip'
    },
    {
      file: 'Asistent-2.7.6-alpha-1.zip',
      url:  'https://github.com/Technic45-creator/Asistent/releases/download/Asistent-2.7.6/Asistent-2.7.6-alpha-1.zip'
    }
  ]
};
