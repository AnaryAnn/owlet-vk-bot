/* В проекте дайджестов переименовать исходные doPost и runWeeklyArtifactBonus_
 * в owletLegacyPost и owletLegacyWeeklyArtifactBonus_. Затем добавить этот файл.
 */
function doPost(e) {
  const data = JSON.parse((e.postData && e.postData.contents) || '{}');
  const password = PropertiesService.getScriptProperties().getProperty('DIGEST_PASSWORD');
  if (!data.password || data.password !== password || data.action !== 'rollArtifact') {
    return owletLegacyPost(e);
  }
  return owletWithSharedWriteLock_(function() { return owletLegacyPost(e); });
}

function runWeeklyArtifactBonus_(threshold, rarity, campaign, preview) {
  return owletWithSharedWriteLock_(function() {
    return owletLegacyWeeklyArtifactBonus_(threshold, rarity, campaign, preview);
  });
}
