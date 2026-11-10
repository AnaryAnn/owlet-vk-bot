/* Общая блокировка Python и Apps Script. Добавить в ОБА проекта.
 * Замок не протухает автоматически. Не удалять его, пока запрос может исполняться.
 * Ни паролей, ни ключей в этом файле нет.
 */
const OWLET_WRITE_LOCK_ID = 1937152401;
const OWLET_WRITE_READY_ID = 1937152402;

function owletBookId_() {
  return typeof book === 'function'
    ? book().getId() : SpreadsheetApp.getActiveSpreadsheet().getId();
}

function owletSheetsRequest_(method, suffix, body) {
  const response = UrlFetchApp.fetch(
    'https://sheets.googleapis.com/v4/spreadsheets/' + owletBookId_() + suffix,
    {method:method, contentType:'application/json',
      headers:{Authorization:'Bearer ' + ScriptApp.getOAuthToken()},
      payload:body === undefined ? undefined : JSON.stringify(body),
      muteHttpExceptions:true});
  const status = response.getResponseCode();
  return {status:status, data:JSON.parse(response.getContentText() || '{}')};
}

function owletWithSharedWriteLock_(operation) {
  const owner = Utilities.getUuid();
  const metadata = {metadataId:OWLET_WRITE_LOCK_ID,
    metadataKey:'owlet_shared_write_v1', metadataValue:owner,
    location:{spreadsheet:true}, visibility:'DOCUMENT'};
  const acquired = owletSheetsRequest_('post', ':batchUpdate', {
    requests:[{createDeveloperMetadata:{developerMetadata:metadata}}]});
  if (acquired.status !== 200) {
    throw new Error('Таблица занята или блокировка требует проверки. HTTP ' + acquired.status);
  }
  try {
    return operation();
  } finally {
    // Даже при ошибке валидации завершаем очередь SpreadsheetApp до снятия замка.
    // Если flush не подтвердился или исполнение прервано, замок остаётся.
    SpreadsheetApp.flush();
    const released = owletSheetsRequest_('post', ':batchUpdate', {requests:[{
      deleteDeveloperMetadata:{dataFilter:{developerMetadataLookup:{
        metadataId:OWLET_WRITE_LOCK_ID, metadataKey:'owlet_shared_write_v1', metadataValue:owner}}}
    }]});
    if (released.status !== 200) throw new Error('Не удалось снять блокировку записи');
  }
}

// Выполнить вручную ПОСЛЕ публикации новых версий Web App в обоих проектах.
function confirmOwletSharedWriteReady() {
  const existing = owletSheetsRequest_('get', '/developerMetadata/' + OWLET_WRITE_READY_ID);
  if (existing.status === 200) {
    if (existing.data.metadataKey !== 'owlet_shared_write_ready' || existing.data.metadataValue !== 'v1') {
      throw new Error('ID метаданных уже занят другим значением');
    }
    return;
  }
  if (existing.status !== 404) throw new Error('Не удалось проверить готовность общей блокировки');
  const added = owletSheetsRequest_('post', ':batchUpdate', {requests:[{
    createDeveloperMetadata:{developerMetadata:{metadataId:OWLET_WRITE_READY_ID,
      metadataKey:'owlet_shared_write_ready', metadataValue:'v1',
      location:{spreadsheet:true}, visibility:'DOCUMENT'}}
  }]});
  if (added.status !== 200) throw new Error('Не удалось отметить готовность общей блокировки');
}
