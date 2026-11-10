/* В проекте статистики переименовать исходные doGet/doPost
 * в owletLegacyGet/owletLegacyPost, затем добавить этот файл.
 */
function doGet(e) {
  if (!checkPassword((e.parameter || {}).password)) return owletLegacyGet(e);
  return owletWithSharedWriteLock_(function() { return owletLegacyGet(e); });
}

function doPost(e) {
  const data = JSON.parse((e.postData && e.postData.contents) || '{}');
  if (!checkPassword(data.password)) return owletLegacyPost(e);
  return owletWithSharedWriteLock_(function() { return owletLegacyPost(e); });
}
