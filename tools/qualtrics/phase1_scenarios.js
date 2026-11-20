/* Relational Fluency, Phase 1 survey: save which scenario each of the
 * participant's four conversations was, as embedded data.
 *   rf_scenario_1..4    e.g. S4A, S1A, S3A, S2A (the run's counterbalanced order)
 *   rf_completed        how many of the four the participant finished (0-4)
 *   rf_scenario_status  ok | missing (no run for this response, or the app
 *                       could not be reached; the analysis join still has it)
 *
 * Paste it unchanged into the JavaScript of the one question on its own page
 * placed right after the completion-code page.
 * docs/qualtrics-phase1-scenarios.md says where. It hides Next while it asks
 * the app (a few seconds at most), then shows it whatever the answer, so a
 * participant is never stuck here.
 */
Qualtrics.SurveyEngine.addOnload(function () {
  var APP = 'https://rf.ai-ready-workforce.ai.cornell.edu';
  var QID = '${e://Field/ResponseID}';
  var q = this;
  q.hideNextButton();

  // Both layouts: setEmbeddedData (Classic, Modern, Flat) and
  // setJSEmbeddedData (Simple, which stores the field as __js_<name>).
  function save(name, value) {
    var v = value == null ? '' : String(value);
    var se = Qualtrics.SurveyEngine;
    try { if (typeof se.setEmbeddedData === 'function') se.setEmbeddedData(name, v); } catch (e) {}
    try { if (typeof se.setJSEmbeddedData === 'function') se.setJSEmbeddedData(name, v); } catch (e) {}
  }

  var done = false;
  function finish(status) {
    if (done) return;
    done = true;
    save('rf_scenario_status', status);
    q.showNextButton();
  }
  setTimeout(function () { finish('missing'); }, 8000);

  fetch(APP + '/api/survey/scenarios?qid=' + encodeURIComponent(QID), { credentials: 'omit' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (!d || done) return finish('missing');
      for (var i = 1; i <= 4; i++) save('rf_scenario_' + i, d['rf_scenario_' + i]);
      save('rf_completed', d.rf_completed);
      finish('ok');
    })
    .catch(function () { finish('missing'); });
});
