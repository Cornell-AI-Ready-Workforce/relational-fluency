/* Relational Fluency: the JavaScript for each conversation block's question
 * in the Qualtrics survey. Paste it unchanged into all four blocks (question
 * > JavaScript); docs/qualtrics-embed.md says where.
 *
 * It reads everything it needs from the block's own frame, the
 * <iframe data-rf src=".../start?...&embed=qualtrics&step=N">: the app's origin
 * (only messages from there count) and the block's step. It hides Next until
 * the app says this block is over, and saves what the app reports as embedded
 * data:
 *   rf_run              the app's run id
 *   rf_scenario_1..4    which scenario each conversation was (e.g. S3A)
 *   rf_status           in_progress | complete | withdrawn | encounter_failed | ...
 *   rf_code             the completion code (the last block), or the code so
 *                       far when the study ended early
 */
Qualtrics.SurveyEngine.addOnload(function () {
  var q = this;
  var box = q.getQuestionContainer ? q.getQuestionContainer() : document;
  var frame = (box && box.querySelector('iframe[data-rf]')) || document.querySelector('iframe[data-rf]');
  if (!frame) return;   // no frame here: leave Next alone
  var src = new URL(frame.getAttribute('src'), location.href);
  var APP_ORIGIN = src.origin;
  var STEP = parseInt(src.searchParams.get('step'), 10) || 0;

  q.hideNextButton();

  // Both layouts: setEmbeddedData (Classic, Modern, Flat) and
  // setJSEmbeddedData (Simple, which stores the field as __js_<name>).
  function save(name, value) {
    var v = value == null ? '' : String(value);
    var se = Qualtrics.SurveyEngine;
    try { if (typeof se.setEmbeddedData === 'function') se.setEmbeddedData(name, v); } catch (e) {}
    try { if (typeof se.setJSEmbeddedData === 'function') se.setJSEmbeddedData(name, v); } catch (e) {}
  }

  function onMessage(e) {
    if (e.origin !== APP_ORIGIN || e.source !== frame.contentWindow) return;
    var m = e.data || {};
    if (m.source !== 'relational-fluency') return;
    if (m.run) save('rf_run', m.run);
    if (m.type === 'conversation_complete' && m.position >= STEP) {
      if (m.scenario) save('rf_scenario_' + m.position, m.scenario);
      save('rf_status', 'in_progress');
      q.showNextButton();
    } else if (m.type === 'run_complete') {
      if (m.scenario) save('rf_scenario_' + (m.position || STEP), m.scenario);
      save('rf_code', m.code);
      save('rf_status', 'complete');
      q.showNextButton();
    } else if (m.type === 'study_ended') {
      save('rf_status', m.status || 'ended');
      if (m.code) save('rf_code', m.code);
      q.showNextButton();
    }
  }
  window.addEventListener('message', onMessage);
  Qualtrics.SurveyEngine.addOnUnload(function () {
    window.removeEventListener('message', onMessage);
  });
});
