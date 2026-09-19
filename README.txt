СЫЧИНАЯ ОХОТА - VK BOT V1

Бот принимает только:
30, 17

Первое число = план.
Второе число = факт.

ФАЙЛЫ
app.py - бот для Render
requirements.txt - зависимости
Code.gs - обновление существующего Google Apps Script

1. GOOGLE APPS SCRIPT

- Замените Code.gs на файл из архива.
- Впишите ваш ТЕКУЩИЙ пароль в SITE_PASSWORD.
- Сохраните.
- Запустите setup() ОДИН РАЗ.
  Появится лист "Участники" с 102 именами и колонкой VK ID.
- Создайте НОВУЮ ВЕРСИЮ текущего Web App deployment.
- URL Apps Script менять не нужно.

В листе "Участники":
A = имя
B = VK ID

2. GITHUB

Создайте отдельный репозиторий, например:
sychnaya-ohota-vk-bot

Загрузите туда:
app.py
requirements.txt

Code.gs в репозиторий бота загружать не нужно.

3. RENDER

New -> Web Service -> выберите репозиторий бота.

Runtime: Python 3
Build Command:
pip install -r requirements.txt

Start Command:
gunicorn app:app

Environment Variables:

VK_TOKEN
секретный токен сообщества VK

VK_GROUP_ID
числовой ID сообщества

GOOGLE_SCRIPT_URL
URL вашего Google Apps Script /exec

GOOGLE_SCRIPT_PASSWORD
тот же пароль, который записан в SITE_PASSWORD в Code.gs

VK_CONFIRMATION_CODE
пока можно не заполнять. Код возьмем из VK на шаге подключения Callback API.

После Deploy Render даст адрес вида:
https://имя-сервиса.onrender.com

Проверка:
откройте этот адрес в браузере.
Должно показать JSON с ok=true.

4. VK CALLBACK API

В управлении сообществом:
Работа с API -> Callback API.

Адрес сервера:
https://ВАШ-СЕРВИС.onrender.com/vk

VK покажет строку подтверждения.
Скопируйте ее в Render:
VK_CONFIRMATION_CODE = эта строка

Сохраните Environment Variable и дождитесь redeploy/restart.
После этого в VK нажмите "Подтвердить".

В типах событий включите:
Входящее сообщение / message_new

5. ПРИВЯЗКА УЧАСТНИКА

Если неизвестный участник пишет:
30, 17

бот отвечает его VK ID.

Организатор открывает лист "Участники", находит имя человека и вставляет VK ID в колонку B.

Следующее сообщение этого человека уже запишется в недельный лист.

ВАЖНО
VK_TOKEN и GOOGLE_SCRIPT_PASSWORD не загружать в GitHub.
Они хранятся только в Environment Variables Render.
