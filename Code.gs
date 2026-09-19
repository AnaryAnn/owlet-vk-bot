const SPREADSHEET_ID = '1S1tcMNZj84rLAQi_zPrPT-yxP0b6OYXHpo-JLbbbH60';
const SITE_PASSWORD = 'ВПИШИТЕ_СЮДА_ВАШ_ТЕКУЩИЙ_ПАРОЛЬ';

const PEOPLE = [
  "Александра Александрова",
  "Александра Иванова",
  "Александра Прохорова",
  "Александра Трубаченкова",
  "Алёна Зиборова",
  "Алина Серова",
  "Алла Сергеева",
  "Алла Шевченко",
  "Анастасия Жумаева",
  "Анастасия Мшвелидзе",
  "Анастасия Широкова",
  "Анна Вотякова",
  "Анна Платонова",
  "Анна Севостьянова",
  "Анна Трейкалс",
  "Анна Фазлеева",
  "Валентина Семикина",
  "Валерия Малахова",
  "Вера Бирюкова",
  "Вероника Чайковская",
  "Виктория Архипова",
  "Виктория Сандакова",
  "Виктория Фирсова",
  "Дарья Сысоева",
  "Дина Залимова",
  "Евгения Емелина",
  "Евгения Кутепова",
  "Евгения Ляпина",
  "Екатерина Апанасенко",
  "Екатерина Карпова",
  "Екатерина Муравьева",
  "Екатерина Севостьянова",
  "Екатерина Сенютина",
  "Екатерина Уланова",
  "Елена Иванова",
  "Елена Канышко",
  "Елена Катулина",
  "Елена Мальцева",
  "Елена Николаева",
  "Елена Перминова",
  "Елена Славина",
  "Елена Степанова",
  "Жанна Шатохина",
  "Ирина Дюдина",
  "Ирина Зеленцова",
  "Катюшка Шарикова",
  "Катя Барская",
  "Ксения Иликаева",
  "Ксения Медведева",
  "Лана Николаева",
  "Лариса Карпухина",
  "Леля Худякова",
  "Лиля Димаровская",
  "Любовь Дедова",
  "Любовь Назаренко",
  "Любовь Рекадзе",
  "Майя Шишигина",
  "Марина Васильева",
  "Марина Глинских",
  "Марина Левшунова",
  "Марина Паутова",
  "Мария Бурдина",
  "Мария Пильгаева",
  "Мария Чикова",
  "Надежда Бураева",
  "Надежда Сивакова",
  "Надя Ефимова",
  "Наталья Абрамова",
  "Наталья Исаченко",
  "Наталья Курникова",
  "Наталья Лабушева",
  "Наталья Панкина",
  "Наталья Скворцова",
  "Наталья Феклина",
  "Нелли Сидоренко",
  "Оксана Вышварина",
  "Ольга Дмитриева",
  "Ольга Кочурова",
  "Ольга Маркова",
  "Ольга Юмагужина",
  "Полина Симоник",
  "Светлана Гаврилова",
  "Светлана Данилович",
  "Светлана Иванова",
  "Светлана Муратова",
  "Скарлетт О'Хара",
  "Тамара Татьянина",
  "Таня Жеребятникова",
  "Татьяна Демиденко",
  "Татьяна Егорова",
  "Татьяна Зайцева",
  "Татьяна Романова",
  "Ульяна Чумакова",
  "Юлия Ануфриченкова",
  "Юлия Березина",
  "Юлия Каримова",
  "Юлия Нужденовская",
  "Юлия Спасова",
  "Eileen White",
  "Lada Dym",
  "Maria Ya",
  "Tatia Na"
];

const META_SHEET = '_Служебное';
const PARTICIPANTS_SHEET = 'Участники';
const FIRST_DATA_ROW = 8;

function checkPassword(p) {
  return String(p || '') === SITE_PASSWORD;
}

function book() {
  return SpreadsheetApp.openById(SPREADSHEET_ID);
}

function isoDate(d) {
  return Utilities.formatDate(d, Session.getScriptTimeZone(), 'yyyy-MM-dd');
}

function prettyDate(d) {
  return Utilities.formatDate(d, Session.getScriptTimeZone(), 'dd.MM.yyyy');
}

function getWeek(date) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  const diff = (d.getDay() - 2 + 7) % 7;

  const start = new Date(d);
  start.setDate(d.getDate() - diff);

  const end = new Date(start);
  end.setDate(start.getDate() + 6);

  return {
    start: isoDate(start),
    end: isoDate(end),
    startDisplay: prettyDate(start),
    endDisplay: prettyDate(end),
    sheetName:
      Utilities.formatDate(start, Session.getScriptTimeZone(), 'dd.MM') +
      '-' +
      Utilities.formatDate(end, Session.getScriptTimeZone(), 'dd.MM.yy')
  };
}


function getParticipantsSheet() {
  let sh = book().getSheetByName(PARTICIPANTS_SHEET);

  if (!sh) {
    sh = book().insertSheet(PARTICIPANTS_SHEET);
    sh.getRange('A1:B1').setValues([['Имя', 'VK ID']]).setFontWeight('bold');
    sh.getRange(2, 1, PEOPLE.length, 2)
      .setValues(PEOPLE.map(name => [name, '']));
    sh.setFrozenRows(1);
    sh.setColumnWidth(1, 220);
    sh.setColumnWidth(2, 150);
    return sh;
  }

  const last = Math.max(sh.getLastRow(), 1);
  const rows = last > 1 ? sh.getRange(2, 1, last - 1, 2).getValues() : [];
  const ids = {};
  rows.forEach(r => {
    const name = String(r[0] || '').trim();
    if (name) ids[name] = r[1];
  });

  sh.getRange(2, 1, Math.max(last - 1, PEOPLE.length), 2).clearContent();
  sh.getRange(2, 1, PEOPLE.length, 2)
    .setValues(PEOPLE.map(name => [name, ids[name] || '']));

  return sh;
}

function findNameByVkId(vkId) {
  const target = String(vkId || '').trim();
  if (!target) return null;

  const sh = getParticipantsSheet();
  const rows = sh.getRange(2, 1, PEOPLE.length, 2).getValues();

  for (let i = 0; i < rows.length; i++) {
    if (String(rows[i][1] || '').trim() === target) {
      return String(rows[i][0] || '').trim();
    }
  }

  return null;
}

function getMetaSheet() {
  let sh = book().getSheetByName(META_SHEET);

  if (!sh) {
    sh = book().insertSheet(META_SHEET);
    sh.appendRow(['Лист', 'Неделя с', 'Неделя по']);
    sh.hideSheet();
  }

  return sh;
}

function registerWeek(week) {
  const meta = getMetaSheet();

  if (meta.getLastRow() >= 2) {
    const names = meta.getRange(2, 1, meta.getLastRow() - 1, 1)
      .getValues().flat().map(String);

    if (names.includes(week.sheetName)) return;
  }

  meta.appendRow([week.sheetName, week.start, week.end]);
}


function syncPeopleInSheet(sh) {
  const lastRow = Math.max(sh.getLastRow(), FIRST_DATA_ROW - 1);
  const existingCount = Math.max(0, lastRow - FIRST_DATA_ROW + 1);
  const existing = existingCount
    ? sh.getRange(FIRST_DATA_ROW, 1, existingCount, 4).getValues()
    : [];

  const saved = {};
  existing.forEach(r => {
    const name = String(r[0] || '').trim();
    if (name) saved[name] = [name, r[1], r[2], r[3]];
  });

  const currentNames = existing.map(r => String(r[0] || '').trim()).filter(Boolean);
  const alreadyCorrect =
    currentNames.length === PEOPLE.length &&
    PEOPLE.every((name, i) => currentNames[i] === name);

  if (alreadyCorrect) return;

  if (existingCount) {
    sh.getRange(FIRST_DATA_ROW, 1, existingCount, 4).clearContent();
  }

  const rows = PEOPLE.map(name => saved[name] || [name, '', '', '']);
  sh.getRange(FIRST_DATA_ROW, 1, rows.length, 4).setValues(rows);
}

function ensureWeekSheet(week) {
  let sh = book().getSheetByName(week.sheetName);

  if (sh) {
    syncPeopleInSheet(sh);
    const lastDataRow = FIRST_DATA_ROW + PEOPLE.length - 1;
    sh.getRange('B3').setFormula('=SUM(B' + FIRST_DATA_ROW + ':B' + lastDataRow + ')');
    sh.getRange('B4').setFormula('=SUM(C' + FIRST_DATA_ROW + ':C' + lastDataRow + ')');
    sh.getRange('B5').setFormula('=IF(B2="","",MAX(0,B2-B3))');
    sh.getRange('B6').setFormula('=IF(B2="","",MAX(0,ROUNDUP((B2-B3)/' + PEOPLE.length + ',0)))');
    registerWeek(week);
    return sh;
  }

  sh = book().insertSheet(week.sheetName);

  sh.getRange('A1:D1').merge();
  sh.getRange('A1')
    .setValue('Неделя ' + week.startDisplay + ' - ' + week.endDisplay)
    .setFontWeight('bold')
    .setFontSize(14);

  sh.getRange('A2').setValue('Цель команды').setFontWeight('bold');
  sh.getRange('B2').setValue('');

  sh.getRange('A3').setValue('Суммарный план').setFontWeight('bold');
  sh.getRange('B3').setFormula('=SUM(B' + FIRST_DATA_ROW + ':B' + (FIRST_DATA_ROW + PEOPLE.length - 1) + ')');

  sh.getRange('A4').setValue('Суммарный факт').setFontWeight('bold');
  sh.getRange('B4').setFormula('=SUM(C' + FIRST_DATA_ROW + ':C' + (FIRST_DATA_ROW + PEOPLE.length - 1) + ')');

  sh.getRange('A5').setValue('Не хватает до цели').setFontWeight('bold');
  sh.getRange('B5').setFormula('=IF(B2="","",MAX(0,B2-B3))');

  sh.getRange('A6').setValue('Добавить к плану на человека').setFontWeight('bold');
  sh.getRange('B6').setFormula('=IF(B2="","",MAX(0,ROUNDUP((B2-B3)/' + PEOPLE.length + ',0)))');

  sh.getRange('A7:D7')
    .setValues([['Имя', 'План', 'Факт', 'Последнее изменение']])
    .setFontWeight('bold');

  sh.getRange(FIRST_DATA_ROW, 1, PEOPLE.length, 4)
    .setValues(PEOPLE.map(name => [name, '', '', '']));

  sh.setFrozenRows(7);
  sh.setColumnWidth(1, 220);
  sh.setColumnWidth(2, 100);
  sh.setColumnWidth(3, 100);
  sh.setColumnWidth(4, 190);

  registerWeek(week);
  return sh;
}

function setup() {
  getMetaSheet();
  getParticipantsSheet();
  ensureWeekSheet(getWeek(new Date()));
}

function personRow(sh, name) {
  const names = sh.getRange(FIRST_DATA_ROW, 1, PEOPLE.length, 1)
    .getValues().flat();

  const index = names.indexOf(name);
  return index < 0 ? null : FIRST_DATA_ROW + index;
}

function getRecord(name, week) {
  const sh = ensureWeekSheet(week);
  const row = personRow(sh, name);

  if (!row) return null;

  const values = sh.getRange(row, 2, 1, 3).getValues()[0];

  if (values[0] === '' && values[1] === '') return null;

  return {
    plan: values[0] === '' ? 0 : Number(values[0]),
    fact: values[1] === '' ? 0 : Number(values[1]),
    updated: values[2] instanceof Date
      ? values[2].toISOString()
      : String(values[2] || '')
  };
}

function saveRecord(name, plan, fact, week) {
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);

  try {
    const sh = ensureWeekSheet(week);
    const row = personRow(sh, name);

    if (!row) throw new Error('Участник не найден');

    const moscowTime = Utilities.formatDate(
      new Date(),
      'Europe/Moscow',
      'dd.MM.yyyy HH:mm:ss'
    );

    sh.getRange(row, 2, 1, 3)
      .setValues([[plan, fact, moscowTime]]);
  } finally {
    lock.releaseLock();
  }
}

function getGoal(week) {
  const value = ensureWeekSheet(week).getRange('B2').getValue();
  return value === '' ? null : Number(value);
}

function saveGoal(goal, week) {
  ensureWeekSheet(week).getRange('B2').setValue(goal);
}

function recordsFromSheet(sh, weekStart, weekEnd) {
  return sh.getRange(FIRST_DATA_ROW, 1, PEOPLE.length, 4)
    .getValues()
    .filter(r => r[1] !== '' || r[2] !== '')
    .map(r => ({
      weekStart: weekStart,
      weekEnd: weekEnd,
      name: String(r[0]),
      plan: r[1] === '' ? 0 : Number(r[1]),
      fact: r[2] === '' ? 0 : Number(r[2]),
      updated: r[3] instanceof Date
        ? r[3].toISOString()
        : String(r[3] || '')
    }));
}

function normalizeMetaDate(value) {
  if (!value) return '';
  if (value instanceof Date) return isoDate(value);

  const text = String(value);
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;

  const parsed = new Date(value);
  return isNaN(parsed.getTime()) ? text : isoDate(parsed);
}

function getAllWeeks() {
  const meta = getMetaSheet();

  if (meta.getLastRow() < 2) return [];

  const rows = meta.getRange(2, 1, meta.getLastRow() - 1, 3).getValues();
  const seen = {};
  const result = [];

  rows.forEach(r => {
    const sheetName = String(r[0] || '');
    const start = normalizeMetaDate(r[1]);
    const end = normalizeMetaDate(r[2]);

    if (!sheetName || seen[sheetName]) return;

    const sh = book().getSheetByName(sheetName);
    if (!sh) return;

    seen[sheetName] = true;

    const goalValue = sh.getRange('B2').getValue();

    result.push({
      sheetName: sheetName,
      weekStart: start,
      weekEnd: end,
      goal: goalValue === '' ? null : Number(goalValue),
      records: recordsFromSheet(sh, start, end)
    });
  });

  return result.sort(
    (a, b) => String(b.weekStart).localeCompare(String(a.weekStart))
  );
}

// Запустить ОДИН РАЗ, если в старом "Лист1" уже есть записи.
function migrateOldData() {
  const old = book().getSheetByName('Лист1');

  if (!old || old.getLastRow() < 2) return;

  const rows = old.getRange(2, 1, old.getLastRow() - 1, 6).getValues();

  rows.forEach(r => {
    const name = String(r[2] || '');
    if (!PEOPLE.includes(name)) return;

    const start = r[0] instanceof Date ? r[0] : new Date(r[0]);
    const end = r[1] instanceof Date ? r[1] : new Date(r[1]);

    if (isNaN(start.getTime()) || isNaN(end.getTime())) return;

    const week = {
      start: isoDate(start),
      end: isoDate(end),
      startDisplay: prettyDate(start),
      endDisplay: prettyDate(end),
      sheetName:
        Utilities.formatDate(start, Session.getScriptTimeZone(), 'dd.MM') +
        '-' +
        Utilities.formatDate(end, Session.getScriptTimeZone(), 'dd.MM.yy')
    };

    const sh = ensureWeekSheet(week);
    const row = personRow(sh, name);

    if (!row) return;

    sh.getRange(row, 2, 1, 3).setValues([[
      Number(r[3]) || 0,
      Number(r[4]) || 0,
      r[5] || new Date()
    ]]);
  });
}

function doGet(e) {
  try {
    if (!checkPassword(e.parameter.password)) {
      return json({
        success: false,
        unauthorized: true,
        error: 'Неверный пароль'
      });
    }

    const action = e.parameter.action || '';

    if (action === 'login') {
      return json({success: true});
    }

    if (action === 'vkUser') {
      const vkId = String(e.parameter.vkId || '').trim();
      return json({
        success: true,
        vkId: vkId,
        name: findNameByVkId(vkId)
      });
    }

    if (action === 'current') {
      const name = String(e.parameter.name || '');

      if (!PEOPLE.includes(name)) {
        throw new Error('Неизвестное имя');
      }

      const week = getWeek(new Date());

      return json({
        success: true,
        week: week,
        record: getRecord(name, week)
      });
    }

    if (action === 'settings') {
      const week = getWeek(new Date());

      return json({
        success: true,
        week: week,
        teamGoal: getGoal(week)
      });
    }

    if (action === 'stats') {
      const week = getWeek(new Date());

      const currentSheet = ensureWeekSheet(week);

      return json({
        success: true,
        currentWeek: week,
        teamGoal: getGoal(week),
        peopleCount: PEOPLE.length,
        currentRecords: recordsFromSheet(currentSheet, week.start, week.end),
        weeks: getAllWeeks()
      });
    }

    return json({success: true});
  } catch (error) {
    return json({
      success: false,
      error: error.message
    });
  }
}

function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);

    if (!checkPassword(data.password)) {
      return json({
        success: false,
        unauthorized: true,
        error: 'Неверный пароль'
      });
    }

    const week = getWeek(new Date());

    if (data.action === 'vkSave') {
      const vkId = String(data.vkId || '').trim();
      const name = findNameByVkId(vkId);

      if (!name) {
        return json({
          success: false,
          unknownVkUser: true,
          vkId: vkId,
          error: 'VK ID не привязан'
        });
      }

      const plan = Number(data.plan);
      const fact = Number(data.fact);

      if (!Number.isFinite(plan) || plan < 0) throw new Error('Некорректный план');
      if (!Number.isFinite(fact) || fact < 0) throw new Error('Некорректный факт');

      saveRecord(name, plan, fact, week);

      return json({
        success: true,
        name: name,
        plan: plan,
        fact: fact,
        week: week
      });
    }

    if (data.action === 'saveGoal') {
      const goal = Number(data.goal);

      if (!Number.isFinite(goal) || goal < 0) {
        throw new Error('Некорректная цель');
      }

      saveGoal(goal, week);

      return json({
        success: true,
        teamGoal: goal
      });
    }

    const name = String(data.name || '').trim();
    const plan = Number(data.plan);
    const fact = Number(data.fact);

    if (!PEOPLE.includes(name)) throw new Error('Неизвестное имя');
    if (!Number.isFinite(plan) || plan < 0) throw new Error('Некорректный план');
    if (!Number.isFinite(fact) || fact < 0) throw new Error('Некорректный факт');

    saveRecord(name, plan, fact, week);

    return json({
      success: true,
      week: week
    });
  } catch (error) {
    return json({
      success: false,
      error: error.message
    });
  }
}

function json(value) {
  return ContentService
    .createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}
