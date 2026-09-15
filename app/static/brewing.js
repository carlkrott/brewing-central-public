(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = {
    recipes: [],
    devices: [],
    brews: [],
    selectedRecipe: null,
    selectedBrew: null,
    assistantAvailable: false,
    recipeStructured: {
      beverage_type: null,
      initial_fermenter_volume_l: null,
      target_metrics: {},
      culture_profiles: [],
      scheduled_additions: [],
      process_steps: [],
    },
  };
  let appReady = false;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function newClientId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (char) => {
      const random = Math.random() * 16 | 0;
      const value = char === 'x' ? random : (random & 0x3 | 0x8);
      return value.toString(16);
    });
  }

  function formatNumber(value, places = 3) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(places).replace(/\.?0+$/, '') : '—';
  }

  function formatTime(value) {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? date.toLocaleString() : String(value);
  }

  async function api(path, options = {}) {
    const headers = { Accept: 'application/json', ...(options.headers || {}) };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...options, headers });
    let data = null;
    try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok) {
      const detail = data && data.detail ? data.detail : `Request failed (${response.status})`;
      const message = typeof detail === 'string' ? detail : (detail.message || detail.code || `Request failed (${response.status})`);
      const error = new Error(message);
      error.code = typeof detail === 'object' ? detail.code : undefined;
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function setMessage(target, message, isError = false) {
    if (!target) return;
    target.textContent = message;
    target.classList.toggle('is-error', isError);
  }

  function activateTab(tabName, updateLocation = true) {
    const valid = new Set(['dashboard', 'recipes', 'brew']);
    const name = valid.has(tabName) ? tabName : 'dashboard';
    $$('.app-tab').forEach((button) => {
      const selected = button.dataset.tab === name;
      button.setAttribute('aria-selected', selected ? 'true' : 'false');
      button.tabIndex = selected ? 0 : -1;
    });
    $$('.tab-panel').forEach((panel) => {
      panel.hidden = panel.id !== `tab-${name}`;
    });
    document.body.dataset.activeTab = name;
    if (updateLocation && window.location.hash !== `#${name}`) {
      history.replaceState(null, '', `#${name}`);
    }
    if (name === 'dashboard') window.dispatchEvent(new Event('resize'));
    if (name === 'brew' && appReady) {
      loadDevicesAndBrews().catch((error) => {
        setMessage($('#brew-status'), `Could not refresh brew state: ${error.message}`, true);
      });
    }
  }

  function bindTabs() {
    $$('.app-tab').forEach((button, index, buttons) => {
      button.addEventListener('click', () => activateTab(button.dataset.tab));
      button.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        let next = index;
        if (event.key === 'ArrowLeft') next = (index - 1 + buttons.length) % buttons.length;
        if (event.key === 'ArrowRight') next = (index + 1) % buttons.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = buttons.length - 1;
        buttons[next].focus();
        activateTab(buttons[next].dataset.tab);
      });
    });
    window.addEventListener('hashchange', () => activateTab(location.hash.slice(1), false));
    activateTab(location.hash.slice(1), false);
  }

  function scalingSummary(scaling) {
    if (!scaling || scaling.mode === 'linear') return 'linear';
    if (scaling.mode === 'fixed') return 'fixed amount';
    if (scaling.mode === 'power') return `power ${formatNumber(scaling.exponent, 2)}`;
    if (scaling.mode === 'piecewise') return `${(scaling.points || []).length} scale points`;
    return String(scaling.mode || 'linear');
  }

  function renderRecipeList() {
    const list = $('#recipe-list');
    list.replaceChildren();
    if (!state.recipes.length) {
      list.append(node('p', 'empty-copy', 'No recipes saved yet. Create the first recipe.'));
      return;
    }
    state.recipes.forEach((recipe) => {
      const button = node('button', 'recipe-list-item');
      button.type = 'button';
      button.dataset.recipeId = String(recipe.id);
      button.setAttribute('aria-pressed', state.selectedRecipe && state.selectedRecipe.id === recipe.id ? 'true' : 'false');
      button.append(node('strong', '', recipe.name));
      button.append(node('span', '', `${recipe.style || 'Uncategorised'} · ${formatNumber(recipe.base_volume_l, 1)} L`));
      button.addEventListener('click', () => selectRecipe(recipe.id));
      list.append(button);
    });
  }

  function ingredientRow(ingredient = {}) {
    const row = node('div', 'ingredient-row');
    row.dataset.ingredientKey = ingredient.ingredient_key || newClientId();
    row.ingredientPayload = ingredient;
    const fields = [
      ['name', 'Ingredient', 'text', ingredient.name || ''],
      ['quantity', 'Amount', 'number', ingredient.quantity ?? ''],
      ['unit', 'Unit', 'text', ingredient.unit || 'g'],
      ['category', 'Category', 'text', ingredient.category || 'other'],
    ];
    fields.forEach(([key, labelText, type, value]) => {
      const label = node('label', 'compact-field');
      label.append(node('span', '', labelText));
      const input = node('input', `ingredient-${key}`);
      input.type = type;
      input.value = value;
      if (type === 'number') {
        input.min = '0';
        input.step = 'any';
      }
      label.append(input);
      row.append(label);
    });

    const modeLabel = node('label', 'compact-field');
    modeLabel.append(node('span', '', 'Scale rule'));
    const mode = node('select', 'ingredient-mode');
    [['linear', 'Linear'], ['fixed', 'Fixed'], ['power', 'Power'], ['piecewise', 'Piecewise']].forEach(([value, text]) => {
      const option = node('option', '', text);
      option.value = value;
      option.selected = (ingredient.scaling?.mode || 'linear') === value;
      mode.append(option);
    });
    modeLabel.append(mode);
    row.append(modeLabel);

    labelledControl(row, 'Material type', editorSelect('ingredient-material-type', [
      ['water', 'Water'], ['fermentable', 'Fermentable'], ['culture', 'Culture'], ['nutrient', 'Nutrient'],
      ['tannin', 'Tannin'], ['acid', 'Acid'], ['enzyme', 'Enzyme'], ['preservative', 'Preservative'],
      ['fining', 'Fining'], ['flavour', 'Flavour'], ['mineral', 'Mineral'], ['packaging', 'Packaging'], ['other', 'Other'],
    ], ingredient.material_type || 'other'));
    labelledControl(row, 'Purpose', editorInput('ingredient-purpose', ingredient.purpose || ''));
    labelledControl(row, 'Addition stage', editorSelect('ingredient-addition-stage', [
      ['', 'Unspecified'], ['mash', 'Mash'], ['boil', 'Boil'], ['fermenter', 'Fermenter'],
      ['secondary', 'Secondary'], ['keg', 'Keg'], ['bottle', 'Bottle'], ['other', 'Other'],
    ], ingredient.addition_stage || ''));
    labelledControl(row, 'Preparation', editorSelect('ingredient-must-preparation', [
      ['none', 'None'], ['rehydrate', 'Rehydrate'], ['dissolve', 'Dissolve'], ['crush', 'Crush'],
      ['slurry', 'Slurry'], ['custom', 'Custom'],
    ], ingredient.must_preparation || 'none'));
    labelledControl(row, 'Schedule allocation', editorSelect('ingredient-schedule-allocation', [
      ['none', 'None'], ['partial', 'Partial'], ['complete', 'Complete'],
    ], ingredient.schedule_allocation || 'none'));
    labelledControl(row, 'Allergen tags', editorInput('ingredient-allergen-tags', (ingredient.allergen_tags || []).join(', ')));
    labelledControl(row, 'Sensitivity tags', editorInput('ingredient-sensitivity-tags', (ingredient.sensitivity_tags || []).join(', ')));

    const detailLabel = node('label', 'compact-field ingredient-rule-detail');
    detailLabel.append(node('span', '', 'Exponent / points'));
    const detail = node('input', 'ingredient-rule');
    detail.type = 'text';
    detail.placeholder = '0.85 or 1:10,30:280,200:1600';
    if (ingredient.scaling?.mode === 'power') detail.value = ingredient.scaling.exponent ?? 1;
    if (ingredient.scaling?.mode === 'piecewise') {
      detail.value = (ingredient.scaling.points || []).map((point) => `${point.volume_l}:${point.quantity}`).join(',');
    }
    detailLabel.append(detail);
    row.append(detailLabel);

    const remove = node('button', 'danger-outline ingredient-remove', 'Remove');
    remove.type = 'button';
    remove.addEventListener('click', () => {
      row.remove();
      syncRecipeStructured();
      renderStructuredEditors();
    });
    row.append(remove);
    return row;
  }

  const CULTURE_KINDS = [
    ['yeast_strain', 'Yeast strain'], ['mixed_yeast_lab', 'Mixed lab culture'],
    ['wild_capture', 'Wild capture'], ['sourdough_starter', 'Sourdough starter'],
    ['kombucha_scooby', 'Kombucha SCOBY'], ['brett_blend', 'Brett blend'], ['other', 'Other'],
  ];
  const SERIES_KINDS = [
    ['sugar_step', 'Sugar step'], ['nutrient_step', 'Nutrient step'],
    ['tannin_step', 'Tannin step'], ['acid_step', 'Acid step'],
    ['aeration', 'Aeration'], ['manual', 'Manual'],
  ];
  const TRIGGER_KINDS = [
    ['manual', 'Manual / operator'], ['operator_observation', 'Operator observation'],
    ['elapsed_since_brew_start', 'Elapsed hours'], ['gravity_at_or_below', 'Gravity at or below'],
    ['gravity_drop_at_least', 'Gravity drop at least'], ['temperature_in_range', 'Temperature range'],
    ['ph_at_or_below', 'pH at or below'],
  ];

  function labelledControl(parent, labelText, control, className = 'compact-field') {
    const label = node('label', className);
    label.append(node('span', '', labelText), control);
    parent.append(label);
    return control;
  }

  function editorInput(className, value = '', type = 'text') {
    const input = node('input', className);
    input.type = type;
    input.value = value ?? '';
    if (type === 'number') {
      input.min = '0';
      input.step = 'any';
    }
    return input;
  }

  function editorTextarea(className, value = '') {
    const textarea = node('textarea', className);
    textarea.rows = 3;
    textarea.value = value ?? '';
    return textarea;
  }

  function editorSelect(className, options, value = '') {
    const select = node('select', className);
    options.forEach(([optionValue, optionText]) => {
      const option = node('option', '', optionText);
      option.value = optionValue;
      option.selected = optionValue === value;
      select.append(option);
    });
    return select;
  }

  function ingredientReferenceSelect(className, selected = '') {
    const options = [['', 'No ingredient link']];
    $$('.ingredient-row', $('#recipe-ingredients')).forEach((ingredient) => {
      options.push([
        ingredient.dataset.ingredientKey,
        $('.ingredient-name', ingredient)?.value.trim() || ingredient.dataset.ingredientKey,
      ]);
    });
    return editorSelect(className, options, selected || '');
  }

  function cultureReferenceSelect(className, selected = '') {
    const options = [['', 'No culture link']];
    $$('.culture-profile-row', $('#recipe-culture-profiles')).forEach((culture) => {
      options.push([culture.dataset.cultureKey, $('.culture-display-name', culture)?.value.trim() || culture.dataset.cultureKey]);
    });
    return editorSelect(className, options, selected || '');
  }

  function additionReferenceSelect(className, selected = '') {
    const options = [['', 'No addition link']];
    $$('.scheduled-addition-row', $('#recipe-scheduled-additions')).forEach((addition) => {
      options.push([addition.dataset.additionKey, `${$('.addition-series-kind', addition)?.value || 'addition'} #${$('.addition-sequence', addition)?.value || '—'}`]);
    });
    return editorSelect(className, options, selected || '');
  }

  function removeStructuredRow(row) {
    row.remove();
    readStructuredEditors();
    renderStructuredEditors();
  }

  function cultureProfileRow(profile = {}) {
    const row = node('div', 'structured-row culture-profile-row');
    row.dataset.cultureKey = profile.culture_key || newClientId();
    labelledControl(row, 'Display name', editorInput('culture-display-name', profile.display_name || profile.name || ''));
    labelledControl(row, 'Ingredient', ingredientReferenceSelect('culture-ingredient-key', profile.ingredient_key));
    labelledControl(row, 'Culture kind', editorSelect('culture-kind', CULTURE_KINDS, profile.culture_kind || 'other'));
    labelledControl(row, 'Identity', editorSelect('culture-identity-assertion', [
      ['single_strain', 'Single strain'], ['mixed_consortium', 'Mixed consortium'], ['unknown', 'Unknown'],
    ], profile.identity_assertion || 'unknown'));
    labelledControl(row, 'Organism status', editorSelect('culture-organism-status', [
      ['uncharacterized', 'Uncharacterized'], ['catalogued_single', 'Catalogued single'],
      ['catalogued_mixed', 'Catalogued mixed'], ['mixed_unknown', 'Mixed / unknown'],
    ], profile.organism_status || 'uncharacterized'));
    labelledControl(row, 'Source / lot', editorInput('culture-source', profile.source || profile.catalog_reference || ''));
    const remove = node('button', 'danger-outline structured-remove', 'Remove culture');
    remove.type = 'button';
    remove.addEventListener('click', () => removeStructuredRow(row));
    row.append(node('small', 'structured-key', `Key ${row.dataset.cultureKey}`), remove);
    return row;
  }

  function triggerValueFor(addition) {
    const trigger = addition.trigger || {};
    if (trigger.kind === 'temperature_in_range') return `${trigger.min_c ?? ''},${trigger.max_c ?? ''}`;
    if (trigger.kind === 'elapsed_since_brew_start') return trigger.hours ?? '';
    if (trigger.kind === 'gravity_drop_at_least') return trigger.delta ?? '';
    if (trigger.kind === 'gravity_at_or_below') return trigger.gravity ?? '';
    if (trigger.kind === 'ph_at_or_below') return trigger.ph ?? '';
    return '';
  }

  function scheduledAdditionRow(addition = {}) {
    const row = node('div', 'structured-row scheduled-addition-row');
    row.dataset.additionKey = addition.addition_key || newClientId();
    row.dataset.seriesKey = addition.series_key || newClientId();
    labelledControl(row, 'Sequence', editorInput('addition-sequence', addition.sequence || '', 'number'));
    labelledControl(row, 'Series', editorSelect('addition-series-kind', SERIES_KINDS, addition.series_kind || 'manual'));
    labelledControl(row, 'Ingredient', ingredientReferenceSelect('addition-ingredient-key', addition.ingredient_key));
    labelledControl(row, 'Quantity', editorInput('addition-quantity', addition.quantity ?? '', 'number'));
    labelledControl(row, 'Unit', editorInput('addition-unit', addition.unit || 'g'));
    labelledControl(row, 'Trigger', editorSelect('addition-trigger-kind', TRIGGER_KINDS, addition.trigger?.kind || 'manual'));
    labelledControl(row, 'Trigger value', editorInput('addition-trigger-value', triggerValueFor(addition)), 'compact-field wide-field');
    labelledControl(row, 'Instructions', editorTextarea('addition-instructions', addition.instructions || ''), 'compact-field wide-field');
    const remove = node('button', 'danger-outline structured-remove', 'Remove addition');
    remove.type = 'button';
    remove.addEventListener('click', () => removeStructuredRow(row));
    row.append(node('small', 'structured-key', `Key ${row.dataset.additionKey}`), remove);
    return row;
  }

  function processStepRow(step = {}) {
    const row = node('div', 'structured-row process-step-row');
    row.dataset.stepKey = step.step_key || newClientId();
    labelledControl(row, 'Sequence', editorInput('process-sequence', step.sequence || '', 'number'));
    labelledControl(row, 'Phase', editorInput('process-phase', step.phase || 'other'));
    labelledControl(row, 'Method', editorInput('process-method', step.method || ''));
    labelledControl(row, 'Instructions', editorTextarea('process-instructions', step.instructions || ''), 'compact-field wide-field');
    labelledControl(row, 'Addition link', additionReferenceSelect('process-linked-addition-key', step.linked_addition_key));
    labelledControl(row, 'Culture link', cultureReferenceSelect('process-linked-culture-key', step.linked_culture_key));
    const remove = node('button', 'danger-outline structured-remove', 'Remove step');
    remove.type = 'button';
    remove.addEventListener('click', () => removeStructuredRow(row));
    row.append(node('small', 'structured-key', `Key ${row.dataset.stepKey}`), remove);
    return row;
  }

  function readNumberOrNull(value) {
    const number = Number(value);
    return value === '' || !Number.isFinite(number) ? null : number;
  }

  function readTrigger(row, previous = {}) {
    const kind = $('.addition-trigger-kind', row).value;
    const raw = $('.addition-trigger-value', row).value.trim();
    const trigger = { ...(previous.trigger || {}), kind };
    if (kind === 'elapsed_since_brew_start') trigger.hours = readNumberOrNull(raw);
    else if (kind === 'gravity_at_or_below') trigger.gravity = readNumberOrNull(raw);
    else if (kind === 'gravity_drop_at_least') trigger.delta = readNumberOrNull(raw);
    else if (kind === 'ph_at_or_below') trigger.ph = readNumberOrNull(raw);
    else if (kind === 'temperature_in_range') {
      const [minimum, maximum] = raw.split(',').map((item) => readNumberOrNull(item.trim()));
      trigger.min_c = minimum;
      trigger.max_c = maximum;
    }
    return trigger;
  }

  function readStructuredEditors() {
    const priorCultures = state.recipeStructured.culture_profiles || [];
    state.recipeStructured.culture_profiles = $$('.culture-profile-row', $('#recipe-culture-profiles')).map((row) => {
      const previous = priorCultures.find((item) => item.culture_key === row.dataset.cultureKey) || {};
      return {
        ...previous,
        culture_key: row.dataset.cultureKey,
        display_name: $('.culture-display-name', row).value.trim(),
        ingredient_key: $('.culture-ingredient-key', row).value || null,
        culture_kind: $('.culture-kind', row).value,
        identity_assertion: $('.culture-identity-assertion', row).value,
        organism_status: $('.culture-organism-status', row).value,
        source: $('.culture-source', row).value.trim(),
      };
    });
    const priorAdditions = state.recipeStructured.scheduled_additions || [];
    state.recipeStructured.scheduled_additions = $$('.scheduled-addition-row', $('#recipe-scheduled-additions')).map((row, index) => {
      const previous = priorAdditions.find((item) => item.addition_key === row.dataset.additionKey) || {};
      return {
        ...previous,
        addition_key: row.dataset.additionKey,
        series_key: row.dataset.seriesKey,
        sequence: Number($('.addition-sequence', row).value) || index + 1,
        series_kind: $('.addition-series-kind', row).value,
        ingredient_key: $('.addition-ingredient-key', row).value || null,
        quantity: readNumberOrNull($('.addition-quantity', row).value),
        unit: $('.addition-unit', row).value.trim(),
        trigger: readTrigger(row, previous),
        instructions: $('.addition-instructions', row).value.trim(),
      };
    });
    const priorSteps = state.recipeStructured.process_steps || [];
    state.recipeStructured.process_steps = $$('.process-step-row', $('#recipe-process-steps')).map((row, index) => {
      const previous = priorSteps.find((item) => item.step_key === row.dataset.stepKey) || {};
      return {
        ...previous,
        step_key: row.dataset.stepKey,
        sequence: Number($('.process-sequence', row).value) || index + 1,
        phase: $('.process-phase', row).value.trim() || 'other',
        method: $('.process-method', row).value.trim(),
        instructions: $('.process-instructions', row).value.trim(),
        linked_addition_key: $('.process-linked-addition-key', row).value || null,
        linked_culture_key: $('.process-linked-culture-key', row).value || null,
      };
    });
  }

  function renderStructuredEditors() {
    const cultures = $('#recipe-culture-profiles');
    const additions = $('#recipe-scheduled-additions');
    const steps = $('#recipe-process-steps');
    if (!cultures || !additions || !steps) return;
    cultures.replaceChildren(...(state.recipeStructured.culture_profiles || []).map(cultureProfileRow));
    additions.replaceChildren(...(state.recipeStructured.scheduled_additions || []).map(scheduledAdditionRow));
    steps.replaceChildren(...(state.recipeStructured.process_steps || []).map(processStepRow));
  }

  function syncRecipeStructured() {
    state.recipeStructured.beverage_type = $('#recipe-beverage-type').value || null;
    state.recipeStructured.initial_fermenter_volume_l = readNumberOrNull($('#recipe-initial-volume').value);
    const metrics = { ...(state.recipeStructured.target_metrics || {}) };
    const abv = readNumberOrNull($('#recipe-target-abv').value);
    const ph = readNumberOrNull($('#recipe-target-ph').value);
    const sweetness = $('#recipe-target-sweetness').value.trim();
    if (abv === null) delete metrics.abv_percent; else metrics.abv_percent = abv;
    if (ph === null) {
      delete metrics.ph_min;
      delete metrics.ph_max;
    } else {
      metrics.ph_min = ph;
      metrics.ph_max = ph;
    }
    if (!sweetness) delete metrics.sweetness; else metrics.sweetness = sweetness;
    state.recipeStructured.target_metrics = metrics;
    readStructuredEditors();
  }

  function clearRecipeForm() {
    state.selectedRecipe = null;
    state.selectedBrew = null;
    state.recipeStructured = {
      beverage_type: null,
      initial_fermenter_volume_l: null,
      target_metrics: {},
      culture_profiles: [],
      scheduled_additions: [],
      process_steps: [],
    };
    $('#recipe-id').value = '';
    $('#recipe-revision').value = '';
    $('#recipe-name').value = '';
    $('#recipe-style').value = '';
    $('#recipe-description').value = '';
    $('#recipe-base-volume').value = '30';
    $('#recipe-beverage-type').value = '';
    $('#recipe-initial-volume').value = '';
    $('#recipe-target-abv').value = '';
    $('#recipe-target-ph').value = '';
    $('#recipe-target-sweetness').value = 'unknown';
    $('#recipe-notes').value = '';
    $('#recipe-ingredients').replaceChildren(ingredientRow());
    renderStructuredEditors();
    $('#recipe-editor-title').textContent = 'New recipe';
    $('#recipe-save').textContent = 'Save recipe';
    $('#recipe-scale-output').replaceChildren(node('p', 'empty-copy', 'Save the recipe to preview scaled quantities.'));
    setMessage($('#recipe-status'), '');
    // W4/F2: drop every assistant binding from the previous recipe and rotate
    // the binding generation so any in-flight poll AssistantJob closure for a
    // prior selection / approval lineage can no longer mutate this panel.
    // The panel is rebound to a new draft recipe, so:
    //   - the recorded pre-apply snapshot would no longer match the form;
    //   - applied-job id, live job id, audit job id, approved finding ids,
    //     audit scope recipe id/revision, and watchdog all belong to the
    //     previous binding and must not leak across selection;
    //   - the generation token rotates so any stale pollAssistantJob
    //     recursion (token + polled job id) drops out before render or
    //     re-binding. Selection alone is enough to cancel an in-flight
    //     poll even if the old poll was for an empty/finished panel.
    invalidateAssistantRecipeBinding($('.assistant-panel[data-assistant-kind="recipe"]'));
    renderRecipeList();
  }

  function fillRecipeForm(recipe) {
    state.selectedRecipe = recipe;
    state.selectedBrew = null;
    state.recipeStructured = {
      beverage_type: recipe.beverage_type || null,
      initial_fermenter_volume_l: recipe.initial_fermenter_volume_l ?? null,
      target_metrics: recipe.target_metrics || {},
      culture_profiles: recipe.culture_profiles || [],
      scheduled_additions: recipe.scheduled_additions || [],
      process_steps: recipe.process_steps || [],
    };
    $('#recipe-id').value = recipe.id;
    $('#recipe-revision').value = recipe.revision;
    $('#recipe-name').value = recipe.name;
    $('#recipe-style').value = recipe.style || '';
    $('#recipe-description').value = recipe.description || '';
    $('#recipe-base-volume').value = recipe.base_volume_l;
    $('#recipe-beverage-type').value = recipe.beverage_type || '';
    $('#recipe-initial-volume').value = recipe.initial_fermenter_volume_l ?? '';
    $('#recipe-target-abv').value = recipe.target_metrics?.abv_percent ?? '';
    $('#recipe-target-ph').value = recipe.target_metrics?.ph_min ?? recipe.target_metrics?.ph ?? '';
    $('#recipe-target-sweetness').value = recipe.target_metrics?.sweetness || 'unknown';
    $('#recipe-notes').value = recipe.notes || '';
    $('#recipe-ingredients').replaceChildren(...recipe.ingredients.map(ingredientRow));
    renderStructuredEditors();
    $('#recipe-editor-title').textContent = `Edit ${recipe.name}`;
    $('#recipe-save').textContent = 'Save changes';
    // Selecting a different recipe invalidates the previously recorded
    // binding, the same way clearRecipeForm does for a new draft: drop the
    // pre-apply snapshot (it was captured against the prior recipeId /
    // recipeRevision), the applied-job id, the live job id, the audit job
    // id, the approved finding ids, the audit scope, and the watchdog. We
    // also rotate the assistant binding generation so any in-flight
    // pollAssistantJob recursion for the prior selection can no longer
    // rebind or render against this newly selected recipe.
    invalidateAssistantRecipeBinding($('.assistant-panel[data-assistant-kind="recipe"]'));
    renderRecipeList();
  }

  function parsePiecewise(value) {
    const points = value.split(',').filter(Boolean).map((entry) => {
      const [volume, quantity] = entry.split(':').map(Number);
      if (!Number.isFinite(volume) || !Number.isFinite(quantity)) throw new Error('Piecewise points must use volume:quantity pairs');
      return { volume_l: volume, quantity };
    });
    if (points.length < 2) throw new Error('Piecewise scaling needs at least two points');
    return points;
  }

  function recipePayload() {
    syncRecipeStructured();
    const ingredients = $$('.ingredient-row', $('#recipe-ingredients')).map((row) => {
      const mode = $('.ingredient-mode', row).value;
      const scaling = { mode, exponent: null, points: [] };
      const detail = $('.ingredient-rule', row).value.trim();
      if (mode === 'power') {
        scaling.exponent = Number(detail);
        if (!Number.isFinite(scaling.exponent)) throw new Error('Power scaling needs a numeric exponent');
      }
      if (mode === 'piecewise') scaling.points = parsePiecewise(detail);
      const original = row.ingredientPayload || {};
      const tags = (selector) => [...new Set($(selector, row).value.split(',').map((tag) => tag.trim()).filter(Boolean))];
      return {
        ingredient_key: row.dataset.ingredientKey,
        client_key: original.client_key || null,
        name: $('.ingredient-name', row).value,
        quantity: Number($('.ingredient-quantity', row).value),
        unit: $('.ingredient-unit', row).value,
        unit_other: original.unit_other || null,
        category: $('.ingredient-category', row).value,
        material_type: $('.ingredient-material-type', row).value,
        purpose: $('.ingredient-purpose', row).value.trim(),
        addition_stage: $('.ingredient-addition-stage', row).value || null,
        addition_timing: original.addition_timing || '',
        scaling,
        // P2/W4: the recipe GET endpoint returns empty optional objects
        // (product, tannin_detail, nutrient_detail) as `{}` rather than
        // `null`. The proposal validator compares every sensitive leaf
        // against the draft and treats `{} → null` as a change that
        // requires suggestion_basis. Normalise empty optional objects to
        // null so an unchanged proposal passes the sensitive-basis check
        // without spurious evidence requirements.
        product: (original.product && Object.keys(original.product).length) ? original.product : null,
        provenance: original.provenance || '',
        allergen_tags: tags('.ingredient-allergen-tags'),
        other_allergen: original.other_allergen || '',
        sensitivity_tags: tags('.ingredient-sensitivity-tags'),
        other_sensitivity: original.other_sensitivity || '',
        tannin_detail: original.tannin_detail || null,
        nutrient_detail: original.nutrient_detail || null,
        must_preparation: $('.ingredient-must-preparation', row).value,
        preparation_other: original.preparation_other || '',
        schedule_allocation: $('.ingredient-schedule-allocation', row).value,
      };
    });
    if (!ingredients.length) throw new Error('Add at least one ingredient');
    return {
      name: $('#recipe-name').value,
      style: $('#recipe-style').value,
      description: $('#recipe-description').value,
      base_volume_l: Number($('#recipe-base-volume').value),
      beverage_type: state.recipeStructured.beverage_type,
      initial_fermenter_volume_l: state.recipeStructured.initial_fermenter_volume_l,
      target_metrics: state.recipeStructured.target_metrics,
      notes: $('#recipe-notes').value,
      ingredients,
      culture_profiles: state.recipeStructured.culture_profiles,
      scheduled_additions: state.recipeStructured.scheduled_additions,
      process_steps: state.recipeStructured.process_steps,
    };
  }

  // P2/W4: the assistant draft contract (RecipeIngredientDraft in
  // app/assistant_pipeline.py) accepts `custom_unit` rather than the recipe
  // save API's `unit_other`. Both names carry the same string; the recipe
  // save API forbids extras, so we keep `recipePayload()` strictly on the
  // API contract and only mirror the field when handing the same draft to
  // the assistant. This is a deep clone so the original payload's reference
  // identity is preserved.
  function recipeDraftForAssistant() {
    const payload = recipePayload();
    const clone = JSON.parse(JSON.stringify(payload));
    if (Array.isArray(clone.ingredients)) {
      clone.ingredients.forEach((ingredient) => {
        if (ingredient && Object.prototype.hasOwnProperty.call(ingredient, 'unit_other')) {
          ingredient.custom_unit = ingredient.unit_other || '';
          delete ingredient.unit_other;
        }
      });
    }
    return clone;
  }

  async function loadRecipes(selectId = null) {
    const response = await api('/api/recipes');
    state.recipes = response.recipes;
    renderRecipeList();
    populateRecipeSelect();
    if (selectId) await selectRecipe(selectId);
  }

  async function selectRecipe(recipeId) {
    try {
      const recipe = await api(`/api/recipes/${recipeId}`);
      fillRecipeForm(recipe);
      // P2/W4: restore any persisted workflow binding for this recipe by
      // refetching the saved job via GET. Reload-only; no model/research
      // call. Awaited so the panel is fully repainted before renderScaledRecipe
      // and the next selection event observe the restored state.
      const panel = $('.assistant-panel[data-assistant-kind="recipe"]');
      if (panel) await tryRestoreWorkflow(panel);
      await renderScaledRecipe();
    } catch (error) {
      setMessage($('#recipe-status'), error.message, true);
    }
  }

  // P2/W4: workflow reload. The recipe tab persists ONLY bounded identifiers
  // and scope metadata in sessionStorage (recipe id + revision + audit job
  // id + approved finding ids + audit scope recipe id/revision). It never
  // persists the job result, evidence, model output, prompt content, the
  // pre-apply draft, or the ephemeral binding token. On reload the page
  // re-fetches the persisted job via GET /api/assistant/jobs/{id}, validates
  // its scope against the stored binding and the currently selected recipe,
  // and either renders the authoritative result or continues polling under a
  // fresh generation token. The model and research layers are never invoked
  // from this path.
  const WORKFLOW_RELOAD_KEY = 'ispindel.recipe.workflow';

  function readWorkflowState() {
    try {
      const raw = window.sessionStorage.getItem(WORKFLOW_RELOAD_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  function writeWorkflowState(state) {
    try {
      if (!state || Object.keys(state).length === 0) {
        window.sessionStorage.removeItem(WORKFLOW_RELOAD_KEY);
        return;
      }
      window.sessionStorage.setItem(WORKFLOW_RELOAD_KEY, JSON.stringify(state));
    } catch (_) {
      // Storage unavailable (private mode, etc.) — degrade silently.
    }
  }

  // Capture the bounded workflow descriptor for the currently selected recipe.
  // No result, evidence, model output, prompt, draft, or binding token is
  // ever persisted — only identifiers and the audit scope.
  function captureWorkflowSnapshot(panel, extra) {
    const snapshot = Object.assign({}, extra || {});
    if (panel && panel.dataset) {
      snapshot.assistantJobId = panel.dataset.assistantJobId || '';
      snapshot.assistantAuditJobId = panel.dataset.assistantAuditJobId || '';
      snapshot.assistantApprovedFindingIds = panel.dataset.assistantApprovedFindingIds || '';
      snapshot.assistantAuditScopeRecipeId = panel.dataset.assistantAuditScopeRecipeId || '';
      snapshot.assistantAuditScopeRecipeRevision = panel.dataset.assistantAuditScopeRecipeRevision || '';
    }
    return snapshot;
  }

  function persistWorkflowSnapshot(panel, extra) {
    const id = $('#recipe-id').value;
    if (!id) {
      writeWorkflowState({});
      return;
    }
    const revision = $('#recipe-revision').value;
    const stored = readWorkflowState();
    const existing = stored[id] || {};
    stored[id] = Object.assign({}, existing, captureWorkflowSnapshot(panel, Object.assign({ recipeId: id, recipeRevision: revision }, extra || {})));
    writeWorkflowState(stored);
  }

  function dropWorkflowSnapshotForRecipe(recipeId) {
    if (!recipeId) {
      writeWorkflowState({});
      return;
    }
    const stored = readWorkflowState();
    if (recipeId in stored) {
      delete stored[recipeId];
      writeWorkflowState(stored);
    }
  }

  // Try to restore the workflow binding for the currently selected recipe.
  // Reload-only path: never submits, never calls the assistant, never writes
  // the recipe. Only refetches the persisted job (GET, no model/research),
  // validates the binding against the live recipe and the stored audit scope,
  // then either renders the authoritative result or resumes polling under a
  // fresh generation token. On 404 or binding mismatch the recipe's snapshot
  // is dropped and the page makes no mutation.
  async function tryRestoreWorkflow(panel) {
    const id = $('#recipe-id').value;
    if (!id) return false;
    const revision = $('#recipe-revision').value;
    const stored = readWorkflowState();
    const snapshot = stored[id];
    if (!snapshot) return false;
    // Re-bind the audit lineage on the panel so the rewrite action can
    // validate scope / approved findings once the persisted job arrives.
    if (snapshot.assistantAuditJobId) panel.dataset.assistantAuditJobId = snapshot.assistantAuditJobId;
    if (snapshot.assistantApprovedFindingIds) panel.dataset.assistantApprovedFindingIds = snapshot.assistantApprovedFindingIds;
    if (snapshot.assistantAuditScopeRecipeId) panel.dataset.assistantAuditScopeRecipeId = snapshot.assistantAuditScopeRecipeId;
    if (snapshot.assistantAuditScopeRecipeRevision) panel.dataset.assistantAuditScopeRecipeRevision = snapshot.assistantAuditScopeRecipeRevision;
    if (!snapshot.assistantJobId) {
      // No persisted assistant job — only the saved audit lineage survived.
      setMessage($('#recipe-status'), 'Restored saved audit lineage — no assistant job to resume.');
      return true;
    }
    // Authoritative refetch: GET the persisted job so we render from the
    // server's frozen record, never from a cached payload.
    let job;
    try {
      job = await api(`/api/assistant/jobs/${snapshot.assistantJobId}`);
    } catch (error) {
      const detail = String((error && error.message) || error || '');
      if (/404/.test(detail)) {
        dropWorkflowSnapshotForRecipe(id);
        setMessage($('#recipe-status'), 'The persisted workflow job is no longer available — cleared the local snapshot.');
        return false;
      }
      setMessage($('#recipe-status'), `Could not restore workflow: ${detail}`, true);
      return false;
    }
    // Validate the fetched job's scope against the currently selected recipe
    // and the stored audit scope. A mismatch refuses the restore and clears
    // the recipe's snapshot without touching the form or any other recipe.
    const currentRecipeId = normalizeScopeRecipeId(id);
    const currentRevision = normalizeScopeRecipeId(revision);
    const snapshotRevision = normalizeScopeRecipeId(snapshot.recipeRevision);
    const jobScopeRecipeId = normalizeScopeRecipeId(job.scope?.recipe_id);
    const jobScopeRevision = normalizeScopeRecipeId(job.scope?.recipe_revision);
    const storedScopeRecipeId = normalizeScopeRecipeId(snapshot.assistantAuditScopeRecipeId);
    const storedScopeRevision = normalizeScopeRecipeId(snapshot.assistantAuditScopeRecipeRevision);
    if (currentRecipeId == null
      || snapshotRevision !== currentRevision
      || (jobScopeRecipeId != null && jobScopeRecipeId !== currentRecipeId)
      || (storedScopeRecipeId != null && jobScopeRecipeId != null && storedScopeRecipeId !== jobScopeRecipeId)
      || (storedScopeRevision != null && jobScopeRevision != null && storedScopeRevision !== jobScopeRevision)) {
      dropWorkflowSnapshotForRecipe(id);
      setMessage($('#recipe-status'), 'The persisted workflow does not match this recipe — cleared the local snapshot.', true);
      return false;
    }
    // Bind a fresh generation token so any prior in-flight poll cannot race
    // the restored render. The stored token is never reused.
    const freshToken = nextAssistantBindingToken(panel);
    panel.dataset.assistantJobId = job.job_id;
    if (job.status === 'succeeded' || job.status === 'failed') {
      renderAssistantJob(panel, job);
      if (jobScopeRevision != null && jobScopeRevision !== currentRevision) {
        const applyButton = $('[data-assistant-action="apply"]', panel);
        if (applyButton) applyButton.disabled = true;
        setMessage($('#recipe-status'), 'Restored historical workflow review. Run the workflow again before applying changes to this revision.');
      } else {
        setMessage($('#recipe-status'), 'Restored workflow state from reload — review before applying.');
      }
      return true;
    }
    // Queued / running: continue polling the same persisted job under the
    // fresh token. No new POST, no model call, no research call.
    panel.dataset.assistantWatchdogMs = panel.dataset.assistantWatchdogMs || '150000';
    pollAssistantJob(panel, job.job_id, Date.now(), freshToken);
    setMessage($('#recipe-status'), 'Resumed an in-progress workflow job — awaiting completion.');
    return true;
  }

  async function saveRecipe(event) {
    event.preventDefault();
    const status = $('#recipe-status');
    const saveButton = $('#recipe-save');
    // P2/W4: idempotent double-click guard. A second click before the
    // in-flight POST resolves must be a no-op, not a duplicate POST that
    // would create an extra persisted recipe row. Disable the button and
    // set a dataset flag; both must be cleared in the finally block even
    // when the POST errors so the form is never stuck in a busy state.
    if (!saveButton || saveButton.dataset.saveBusy === 'true' || saveButton.disabled) return;
    saveButton.dataset.saveBusy = 'true';
    saveButton.disabled = true;
    try {
      const payload = recipePayload();
      const id = $('#recipe-id').value;
      let saved;
      if (id) {
        payload.expected_revision = Number($('#recipe-revision').value);
        saved = await api(`/api/recipes/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
      } else {
        saved = await api('/api/recipes', { method: 'POST', body: JSON.stringify(payload) });
      }
      setMessage(status, `Saved revision ${saved.revision}.`);
      // P2/W4: a save advances the saved revision. The workflow snapshot
      // is keyed by recipeId; rewrite its recipeRevision so a reload that
      // re-selects this same recipe (now at the new revision) still
      // restores the review surface. Without this update the natural
      // (recipeId, recipeRevision) guard would drop the snapshot after
      // every save and the reload would render an empty review panel.
      try {
        const stored = readWorkflowState();
        const snapshot = stored[saved.id];
        if (snapshot) {
          snapshot.recipeRevision = String(saved.revision);
          writeWorkflowState(stored);
        }
      } catch (_) {
        // Storage unavailable — degrade silently.
      }
      await loadRecipes(saved.id);
    } catch (error) {
      if (error.status === 409) {
        // P2/W4: stale-revision refusal with rebase/reload offer. The
        // operator can reload the recipe to pick up the canonical saved
        // revision and re-run the audit/apply chain against it.
        setMessage(
          status,
          'This recipe changed elsewhere. Reload before overwriting it, or rebase the audit against the saved revision.',
          true,
        );
      } else {
        setMessage(status, error.message, true);
      }
    } finally {
      saveButton.disabled = false;
      delete saveButton.dataset.saveBusy;
    }
  }

  let scaleTimer = null;
  let scaleRequestSequence = 0;
  async function renderScaledRecipe() {
    const requestSequence = ++scaleRequestSequence;
    const target = Number($('#recipe-target-volume').value);
    $('#recipe-target-volume-number').value = target;
    $('#recipe-target-label').textContent = `${formatNumber(target, 1)} L`;
    const output = $('#recipe-scale-output');
    if (!state.selectedRecipe) return;
    try {
      const recipe = await api(`/api/recipes/${state.selectedRecipe.id}?volume_l=${encodeURIComponent(target)}`);
      if (requestSequence !== scaleRequestSequence) return;
      const fragment = document.createDocumentFragment();
      recipe.ingredients.forEach((ingredient) => {
        const item = node('div', 'scale-result');
        item.append(node('strong', '', ingredient.name));
        item.append(node('span', '', `${formatNumber(ingredient.scaled_quantity, 3)} ${ingredient.unit}`));
        item.append(node('small', '', scalingSummary(ingredient.scaling)));
        fragment.append(item);
      });
      output.replaceChildren(fragment);
    } catch (error) {
      if (requestSequence !== scaleRequestSequence) return;
      output.replaceChildren(node('p', 'status is-error', error.message));
    }
  }

  function scheduleScale() {
    clearTimeout(scaleTimer);
    scaleTimer = setTimeout(renderScaledRecipe, 180);
  }

  function populateRecipeSelect() {
    const select = $('#brew-recipe-select');
    const selected = select.value;
    select.replaceChildren();
    const blank = node('option', '', 'Select recipe…');
    blank.value = '';
    select.append(blank);
    state.recipes.forEach((recipe) => {
      const option = node('option', '', recipe.name);
      option.value = recipe.id;
      select.append(option);
    });
    if ($(`option[value="${CSS.escape(selected)}"]`, select)) select.value = selected;
  }

  function populateDeviceSelect() {
    const select = $('#brew-device-select');
    const selected = select.value;
    select.replaceChildren();
    const blank = node('option', '', 'Select iSpindel…');
    blank.value = '';
    select.append(blank);
    state.devices.forEach((device) => {
      const mode = device.operating_mode || 'brewing';
      const stateLabel = mode === 'stored' ? 'stored' : (device.stale ? 'offline/stale' : 'online');
      const option = node('option', '', `${device.device_name || device.device_id} · ${stateLabel}`);
      option.value = device.device_id;
      select.append(option);
    });
    if ($(`option[value="${CSS.escape(selected)}"]`, select)) select.value = selected;
    renderSelectedDevice();
  }

  function renderSelectedDevice() {
    const id = $('#brew-device-select').value;
    const device = state.devices.find((candidate) => candidate.device_id === id);
    const output = $('#brew-device-state');
    output.replaceChildren();
    if (!device) {
      output.append(node('p', 'empty-copy', 'Choose an iSpindel to see its current persisted state.'));
      return;
    }
    const title = node('h3', '', device.device_name || device.device_id);
    const mode = device.operating_mode || 'brewing';
    const status = node('span', `device-status ${mode === 'stored' ? 'is-online' : (device.stale ? 'is-stale' : 'is-online')}`, mode === 'stored' ? 'Stored / intentionally off' : (device.stale ? 'Offline / stale' : 'Online'));
    title.append(status);
    output.append(title);
    const grid = node('dl', 'device-facts');
    const cameraPolicy = device.camera_policy || 'active_brew_structural';
    [['Device ID', device.device_id], ['Operating intent', mode], ['Camera policy', cameraPolicy], ['Last sample', formatTime(device.last_seen)], ['Stored samples', String(device.sample_count ?? 0)], ['Expected interval', `${device.expected_interval_sec ?? '—'} s`]].forEach(([label, value]) => {
      grid.append(node('dt', '', label), node('dd', '', value));
    });
    output.append(grid);
    $('#operating-intent-mode').value = mode;
    $('#operating-intent-camera').value = cameraPolicy;
    $('#operating-intent-status').textContent = device.operating_intent_configured ? `Saved ${formatTime(device.operating_intent_updated_at)}.` : 'Using the safe default until you save an intent.';
    renderActiveBrew();
  }

  async function saveOperatingIntent() {
    const deviceId = $('#brew-device-select').value;
    if (!deviceId) {
      setMessage($('#operating-intent-status'), 'Select an iSpindel first.', true);
      return;
    }
    const button = $('#save-operating-intent');
    button.disabled = true;
    try {
      const intent = await api(`/api/device/${encodeURIComponent(deviceId)}/operating-intent`, {
        method: 'PUT',
        body: JSON.stringify({
          mode: $('#operating-intent-mode').value,
          camera_policy: $('#operating-intent-camera').value,
        }),
      });
      const device = state.devices.find((candidate) => candidate.device_id === deviceId);
      if (device) {
        device.operating_mode = intent.mode;
        device.camera_policy = intent.camera_policy;
        device.operating_intent_configured = intent.configured;
        device.operating_intent_updated_at = intent.updated_at;
      }
      setMessage($('#operating-intent-status'), `Saved: ${intent.mode}; camera ${intent.camera_policy}.`);
      populateDeviceSelect();
    } catch (error) {
      setMessage($('#operating-intent-status'), error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function activeBrewForSelectedDevice() {
    const id = $('#brew-device-select').value;
    return state.brews.find((brew) => brew.device_id === id && brew.status === 'active') || null;
  }

  function scheduledAdditionState(addition, brew) {
    if (addition.state === 'recorded' || addition.state === 'skipped') return addition.state;
    if (addition.state === 'overdue' || addition.state === 'due' || addition.state === 'upcoming') return addition.state;
    if (addition.state === 'blocked') return 'blocked';
    if (addition.state === 'condition_met') return 'condition met';
    const terminal = (brew.events || []).find((event) =>
      ['scheduled_addition_recorded', 'scheduled_addition_skipped'].includes(event.event_type)
      && event.data && event.data.addition_key === addition.addition_key);
    if (terminal) return terminal.event_type === 'scheduled_addition_recorded' ? 'recorded' : 'skipped';
    const trigger = addition.trigger || {};
    if (trigger.kind === 'gravity_at_or_below' || trigger.kind === 'gravity_drop_at_least' || trigger.kind === 'temperature_in_range') return 'condition pending';
    return 'due';
  }

  async function confirmScheduledAddition(brew, addition, action) {
    const status = $('#brew-status');
    const stateLabel = scheduledAdditionState(addition, brew);
    if (stateLabel === 'recorded' || stateLabel === 'skipped') return;
    const title = `${action === 'skip' ? 'Skip' : 'Record'} ${addition.series_kind || 'addition'} #${addition.sequence || '—'}?`;
    if (!window.confirm(`${title}\n${addition.instructions || addition.preparation || addition.ingredient_key || ''}`)) return;
    const data = { addition_key: addition.addition_key };
    let eventType = 'scheduled_addition_recorded';
    if (action === 'skip') {
      const reason = window.prompt('Reason for skipping this addition:', 'Operator skipped');
      if (!reason || !reason.trim()) return;
      eventType = 'scheduled_addition_skipped';
      data.reason = reason.trim();
    } else {
      data.actual_quantity = addition.scaled_quantity ?? addition.quantity;
      data.actual_unit = addition.unit;
    }
    try {
      await api(`/api/brews/${brew.id}/events`, {
        method: 'POST',
        body: JSON.stringify({
          event_type: eventType,
          source: 'agent',
          client_request_id: newClientId(),
          data,
        }),
      });
      setMessage(status, `${action === 'skip' ? 'Skipped' : 'Recorded'} addition ${addition.addition_key}.`);
      await loadDevicesAndBrews();
    } catch (error) {
      setMessage(status, error.message, true);
    }
  }

  function renderScheduledAdditions(brew) {
    const additions = brew.scheduled_additions || brew.recipe_snapshot?.scheduled_additions || [];
    const section = node('section', 'scheduled-additions');
    section.append(node('h3', '', 'Next additions'));
    if (!additions.length) {
      section.append(node('p', 'empty-copy', 'No scheduled additions in this brew snapshot.'));
      return section;
    }
    additions.slice().sort((left, right) => (left.sequence || 0) - (right.sequence || 0)).forEach((addition) => {
      const item = node('article', 'scheduled-addition');
      const stateLabel = scheduledAdditionState(addition, brew);
      item.append(node('strong', '', `${addition.series_kind || 'addition'} · ${addition.sequence || '—'}`));
      item.append(node('span', 'schedule-chip', stateLabel));
      item.append(node('p', '', `${formatNumber(addition.scaled_quantity ?? addition.quantity, 3)} ${addition.unit || ''}`));
      if (addition.instructions) item.append(node('small', '', addition.instructions));
      if (stateLabel !== 'recorded' && stateLabel !== 'skipped') {
        const actions = node('div', 'scheduled-addition-actions');
        const record = node('button', 'secondary', 'Record');
        record.type = 'button';
        record.disabled = ['condition pending', 'blocked', 'upcoming', 'pending'].includes(stateLabel);
        record.addEventListener('click', () => confirmScheduledAddition(brew, addition, 'record'));
        const skip = node('button', 'danger-outline', 'Skip');
        skip.type = 'button';
        skip.addEventListener('click', () => confirmScheduledAddition(brew, addition, 'skip'));
        actions.append(record, skip);
        item.append(actions);
      }
      section.append(item);
    });
    return section;
  }

  function renderActiveBrew() {
    const brew = activeBrewForSelectedDevice();
    state.selectedBrew = brew;
    const output = $('#active-brew');
    output.replaceChildren();
    const begin = $('#begin-brew');
    const stop = $('#stop-brew');
    begin.disabled = !$('#brew-device-select').value || Boolean(brew);
    stop.disabled = !brew;
    if (!brew) {
      output.append(node('p', 'empty-copy', 'No active brew on this device.'));
      return;
    }
    output.append(node('h3', '', brew.recipe_snapshot.name));
    output.append(node('p', '', `${formatNumber(brew.target_volume_l, 1)} L · started ${formatTime(brew.started_at)}`));
    if (brew.notes) output.append(node('p', 'brew-note', brew.notes));
    const events = node('ol', 'event-list');
    brew.events.slice(-6).reverse().forEach((event) => {
      const item = node('li');
      item.append(node('strong', '', event.event_type.replaceAll('_', ' ')));
      item.append(node('span', '', `${formatTime(event.event_at)} · ${event.source}`));
      if (event.notes) item.append(node('p', '', event.notes));
      events.append(item);
    });
    output.append(events);
    output.append(renderScheduledAdditions(brew));
  }

  const ARCHIVE_ANNOTATION_CLASSIFICATIONS = [
    'operator_post_brew',
    'tasting_outcome',
    'hypothesis',
    'annotation_clarification',
  ];
  const ARCHIVE_NOTES_MAX = 4000;

  function freezeArchiveEvidence(brewId) {
    return api(`/api/brews/${brewId}/archive-evidence`, { method: 'POST' });
  }

  async function fetchArchiveAnnotations(brewId) {
    const response = await api(`/api/brews/${brewId}/archive-annotations`);
    return Array.isArray(response.annotations) ? response.annotations : [];
  }

  function archiveReviewRow(label, value) {
    const row = node('div', 'archive-review-row');
    row.append(node('span', 'archive-review-label', label));
    row.append(node('span', 'archive-review-value', value || '—'));
    return row;
  }

  function renderArchiveComparisonPanel(panel, status, brews, bundles, errorMessage) {
    panel.replaceChildren();
    panel.append(node('h4', '', 'Compare selected archived brews'));
    if (status === 'loading') {
      panel.append(node('p', 'archive-review-status', 'Loading frozen evidence for selected brews…'));
      return;
    }
    if (status === 'error') {
      panel.append(node('p', 'archive-review-status is-error', errorMessage || 'Could not load frozen evidence.'));
      return;
    }
    if (status === 'empty') {
      panel.append(node('p', 'archive-review-status', 'Select at least two archived brews and choose Compare selected.'));
      return;
    }
    if (status === 'no-evidence') {
      panel.append(node('p', 'archive-review-status is-error', 'Frozen evidence is not yet available for one or more selected brews.'));
      return;
    }
    if (status !== 'ready') return;
    panel.append(node('p', 'archive-review-status', `Showing ${brews.length} archived brews side-by-side from their frozen evidence bundles.`));
    brews.forEach((brew, index) => {
      const bundle = bundles[index];
      const section = node('section', 'archive-review-item');
      section.append(node('h5', '', brew.recipe_snapshot ? brew.recipe_snapshot.name : `Brew #${brew.id}`));
      section.append(archiveReviewRow('Brew ID', String(brew.id)));
      section.append(archiveReviewRow('Status', brew.status));
      section.append(archiveReviewRow('Target volume', `${formatNumber(brew.target_volume_l, 1)} L`));
      section.append(archiveReviewRow('Started', formatTime(brew.started_at)));
      section.append(archiveReviewRow('Ended', formatTime(brew.ended_at)));
      section.append(archiveReviewRow('Evidence hash', bundle && bundle.evidence_hash ? bundle.evidence_hash : '—'));
      section.append(archiveReviewRow('Captured at', bundle && bundle.captured_at ? formatTime(bundle.captured_at) : '—'));
      const snapshotList = node('ul', 'archive-review-snapshot');
      let snapshotEntries = [];
      try {
        snapshotEntries = bundle && bundle.source_snapshot_json ? JSON.parse(bundle.source_snapshot_json) : [];
      } catch (_) {
        snapshotEntries = [];
      }
      if (snapshotEntries && typeof snapshotEntries === 'object' && !Array.isArray(snapshotEntries)) {
        Object.keys(snapshotEntries).sort().forEach((key) => {
          const item = node('li');
          item.append(node('strong', '', key));
          item.append(node('span', '', String(snapshotEntries[key])));
          snapshotList.append(item);
        });
      }
      section.append(node('h6', '', 'Frozen snapshot keys'));
      section.append(snapshotList);
      const eventsList = node('ol', 'archive-review-events');
      let eventEntries = [];
      try {
        eventEntries = bundle && bundle.source_events_json ? JSON.parse(bundle.source_events_json) : [];
      } catch (_) {
        eventEntries = [];
      }
      if (Array.isArray(eventEntries)) {
        eventEntries.forEach((event) => {
          const item = node('li');
          item.append(node('strong', '', event && event.event_type ? String(event.event_type) : 'event'));
          item.append(node('span', '', `${event && event.event_at ? formatTime(event.event_at) : '—'} · ${event && event.source ? String(event.source) : '—'}`));
          if (event && event.notes) item.append(node('p', '', String(event.notes)));
          eventsList.append(item);
        });
      }
      section.append(node('h6', '', 'Frozen events'));
      section.append(eventsList);
      panel.append(section);
    });
  }

  function selectedArchiveBrewIds() {
    return $$('input.archive-compare-checkbox', $('#brew-archive'))
      .filter((input) => input.checked)
      .map((input) => Number(input.dataset.brewId));
  }

  async function compareSelectedArchivedBrews(button) {
    const panel = $('#archive-compare-panel');
    const ids = selectedArchiveBrewIds();
    button.disabled = true;
    try {
      if (ids.length < 2) {
        renderArchiveComparisonPanel(panel, 'empty', [], [], null);
        return;
      }
      const brews = ids
        .map((id) => state.brews.find((brew) => brew.id === id))
        .filter((brew) => brew && brew.status !== 'active');
      if (brews.length < 2) {
        renderArchiveComparisonPanel(panel, 'empty', [], [], null);
        return;
      }
      renderArchiveComparisonPanel(panel, 'loading', [], [], null);
      const bundles = await Promise.all(brews.map((brew) => freezeArchiveEvidence(brew.id)));
      if (bundles.some((bundle) => !bundle || !bundle.evidence_hash)) {
        renderArchiveComparisonPanel(panel, 'no-evidence', brews, bundles, null);
        return;
      }
      renderArchiveComparisonPanel(panel, 'ready', brews, bundles, null);
    } catch (error) {
      renderArchiveComparisonPanel(panel, 'error', [], [], error.message);
    } finally {
      button.disabled = false;
    }
  }

  function renderAnnotationHistory(card, annotations) {
    const history = $('.archive-annotation-history', card);
    history.replaceChildren();
    if (!annotations.length) {
      history.append(node('p', 'archive-annotation-empty', 'No annotations recorded yet.'));
      return;
    }
    annotations.forEach((entry) => {
      const row = node('article', 'archive-annotation-row');
      row.append(node('strong', '', `${entry.revision_no}. ${String(entry.classification || '')}`));
      row.append(node('span', '', `${entry.origin || '—'} · ${formatTime(entry.created_at)}`));
      const notes = entry && entry.content && typeof entry.content.notes === 'string' ? entry.content.notes : '';
      row.append(node('p', '', notes));
      history.append(row);
    });
  }

  async function loadAnnotationHistory(button, brew, card) {
    button.disabled = true;
    try {
      const annotations = await fetchArchiveAnnotations(brew.id);
      renderAnnotationHistory(card, annotations);
    } catch (error) {
      const history = $('.archive-annotation-history', card);
      history.replaceChildren();
      history.append(node('p', 'archive-annotation-empty is-error', error.message));
    } finally {
      button.disabled = false;
    }
  }

  async function submitArchiveAnnotation(button, brew, card) {
    const select = $('.archive-annotation-classification', card);
    const textarea = $('.archive-annotation-notes', card);
    const status = $('.archive-annotation-status', card);
    const classification = select ? select.value : '';
    const notes = textarea ? textarea.value.trim() : '';
    if (!ARCHIVE_ANNOTATION_CLASSIFICATIONS.includes(classification)) {
      setMessage(status, 'Choose an annotation classification.', true);
      return;
    }
    if (!notes) {
      setMessage(status, 'Annotation notes must not be empty.', true);
      return;
    }
    button.disabled = true;
    try {
      let parentId = null;
      try {
        const history = await fetchArchiveAnnotations(brew.id);
        if (history.length) parentId = history[history.length - 1].id;
      } catch (error) {
        setMessage(status, `Could not load annotation history: ${error.message}`, true);
        button.disabled = false;
        return;
      }
      await api(`/api/brews/${brew.id}/archive-annotations`, {
        method: 'POST',
        body: JSON.stringify({
          classification,
          origin: 'operator',
          content: { notes },
          parent_annotation_id: parentId,
        }),
      });
      setMessage(status, `Annotation recorded (revision ${parentId ? 'appended' : '1'}).`);
      if (textarea) textarea.value = '';
      const refreshed = await fetchArchiveAnnotations(brew.id);
      renderAnnotationHistory(card, refreshed);
    } catch (error) {
      setMessage(status, error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function forkArchiveBrew(button, brew) {
    button.disabled = true;
    try {
      const defaultName = brew.recipe_snapshot && brew.recipe_snapshot.name
        ? `${brew.recipe_snapshot.name} (fork)`
        : `Brew #${brew.id} fork`;
      const rawName = typeof window.prompt === 'function' ? window.prompt('Name the new recipe draft:', defaultName) : defaultName;
      const newName = typeof rawName === 'string' ? rawName.trim() : '';
      if (!newName) {
        button.disabled = false;
        return;
      }
      const payload = {
        source_recipe_id: brew.recipe_id,
        source_recipe_revision: brew.recipe_snapshot && brew.recipe_snapshot.revision,
        source_brew_run_id: brew.id,
        new_name: newName,
      };
      const created = await api('/api/archive/fork-recipe', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      setMessage($('#brew-status'), `Forked brew #${brew.id} into new recipe #${created.id}.`);
      if (typeof loadRecipes === 'function') {
        await loadRecipes(created.id);
      }
    } catch (error) {
      setMessage($('#brew-status'), error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function buildArchiveCard(brew) {
    const card = node('article', 'archive-item');
    card.dataset.brewId = String(brew.id);
    const header = node('div', 'archive-item-header');
    const checkbox = node('input', 'archive-compare-checkbox');
    checkbox.type = 'checkbox';
    checkbox.dataset.brewId = String(brew.id);
    checkbox.setAttribute('aria-label', `Select brew ${brew.id} for comparison`);
    header.append(checkbox);
    header.append(node('h4', '', brew.recipe_snapshot && brew.recipe_snapshot.name ? brew.recipe_snapshot.name : `Brew #${brew.id}`));
    card.append(header);
    card.append(node('p', '', `${brew.status} · ${formatNumber(brew.target_volume_l, 1)} L`));
    card.append(node('small', '', `${formatTime(brew.started_at)} → ${formatTime(brew.ended_at)}`));
    if (brew.notes) card.append(node('p', '', brew.notes));

    const evidenceState = node('p', 'archive-evidence-state', 'Evidence: not frozen yet.');
    const freezeButton = node('button', 'secondary', 'Freeze / review evidence');
    freezeButton.type = 'button';
    freezeButton.addEventListener('click', async () => {
      freezeButton.disabled = true;
      try {
        const bundle = await freezeArchiveEvidence(brew.id);
        if (bundle && bundle.evidence_hash) {
          evidenceState.textContent = `Evidence: ${bundle.evidence_hash.slice(0, 12)}… (captured ${formatTime(bundle.captured_at)})`;
        } else {
          evidenceState.textContent = 'Evidence: not available.';
        }
      } catch (error) {
        evidenceState.textContent = `Evidence error: ${error.message}`;
      } finally {
        freezeButton.disabled = false;
      }
    });
    card.append(freezeButton);
    card.append(evidenceState);

    const contextButton = node('button', 'secondary', 'Use as assistant context');
    contextButton.type = 'button';
    contextButton.addEventListener('click', () => {
      state.selectedBrew = brew;
      setMessage($('#brew-status'), `Assistant context set to archived brew #${brew.id}.`);
    });
    card.append(contextButton);

    const forkButton = node('button', 'secondary', 'Fork as new recipe');
    forkButton.type = 'button';
    forkButton.addEventListener('click', () => forkArchiveBrew(forkButton, brew));
    card.append(forkButton);

    const annotationSection = node('section', 'archive-annotation');
    const classificationLabel = node('label', 'archive-annotation-classification-label', 'Classification');
    const classificationSelect = node('select', 'archive-annotation-classification');
    ARCHIVE_ANNOTATION_CLASSIFICATIONS.forEach((value) => {
      const option = node('option', '', value);
      option.value = value;
      classificationSelect.append(option);
    });
    classificationLabel.append(classificationSelect);
    annotationSection.append(classificationLabel);
    const notesLabel = node('label', 'archive-annotation-notes-label', 'Notes');
    const notesTextarea = node('textarea', 'archive-annotation-notes');
    notesTextarea.rows = 3;
    notesTextarea.maxLength = ARCHIVE_NOTES_MAX;
    notesTextarea.setAttribute('aria-label', 'Annotation notes');
    notesLabel.append(notesTextarea);
    annotationSection.append(notesLabel);
    const annotationStatus = node('p', 'archive-annotation-status');
    annotationSection.append(annotationStatus);
    const submitButton = node('button', 'secondary', 'Add annotation');
    submitButton.type = 'button';
    submitButton.addEventListener('click', () => submitArchiveAnnotation(submitButton, brew, card));
    annotationSection.append(submitButton);
    const reloadButton = node('button', 'secondary', 'Reload annotations');
    reloadButton.type = 'button';
    reloadButton.addEventListener('click', () => loadAnnotationHistory(reloadButton, brew, card));
    annotationSection.append(reloadButton);
    const historyList = node('div', 'archive-annotation-history');
    annotationSection.append(historyList);
    card.append(annotationSection);

    return card;
  }

  function renderArchive() {
    const output = $('#brew-archive');
    output.replaceChildren();
    const archived = state.brews.filter((brew) => brew.status !== 'active');
    const comparePanel = node('section', 'archive-compare-panel');
    comparePanel.id = 'archive-compare-panel';
    const compareButton = node('button', 'secondary', 'Compare selected');
    compareButton.type = 'button';
    compareButton.addEventListener('click', () => compareSelectedArchivedBrews(compareButton));
    const compareRow = node('div', 'archive-compare-controls');
    compareRow.append(compareButton);
    compareRow.append(node('p', 'archive-compare-hint', 'Select two or more archived brews and choose Compare selected to review frozen evidence side-by-side.'));
    output.append(compareRow);
    if (!archived.length) {
      output.append(node('p', 'empty-copy', 'Completed and aborted brews will appear here.'));
      renderArchiveComparisonPanel(comparePanel, 'empty', [], [], null);
      output.append(comparePanel);
      return;
    }
    archived.forEach((brew) => {
      output.append(buildArchiveCard(brew));
    });
    renderArchiveComparisonPanel(comparePanel, 'empty', [], [], null);
    output.append(comparePanel);
  }

  async function loadDevicesAndBrews() {
    const [deviceResponse, brewResponse] = await Promise.all([api('/api/devices'), api('/api/brews')]);
    state.devices = deviceResponse.devices;
    state.brews = brewResponse.brews;
    populateDeviceSelect();
    renderArchive();
  }

  async function beginBrew() {
    const status = $('#brew-status');
    const deviceId = $('#brew-device-select').value;
    const recipeId = Number($('#brew-recipe-select').value);
    const volume = Number($('#brew-volume').value);
    if (!deviceId || !recipeId) {
      setMessage(status, 'Select both an iSpindel and a recipe.', true);
      return;
    }
    const device = state.devices.find((item) => item.device_id === deviceId);
    const recipe = state.recipes.find((item) => item.id === recipeId);
    if (!window.confirm(`Begin ${formatNumber(volume, 1)} L of ${recipe.name} on ${device.device_name || deviceId}? The recipe will be snapshotted.`)) return;
    try {
      const brew = await api('/api/brews', {
        method: 'POST',
        body: JSON.stringify({ device_id: deviceId, recipe_id: recipeId, target_volume_l: volume, notes: $('#brew-notes').value }),
      });
      setMessage(status, `Brew #${brew.id} started.`);
      await loadDevicesAndBrews();
    } catch (error) {
      setMessage(status, error.message, true);
    }
  }

  async function stopBrew() {
    const brew = activeBrewForSelectedDevice();
    if (!brew) return;
    const outcome = $('#brew-outcome').value;
    if (!window.confirm(`${outcome === 'aborted' ? 'Abort' : 'Complete'} brew #${brew.id} (${brew.recipe_snapshot.name}) on ${brew.device_id}?`)) return;
    try {
      await api(`/api/brews/${brew.id}/stop`, {
        method: 'POST',
        body: JSON.stringify({ outcome, notes: $('#brew-stop-notes').value }),
      });
      setMessage($('#brew-status'), `Brew #${brew.id} marked ${outcome}.`);
      await loadDevicesAndBrews();
    } catch (error) {
      setMessage($('#brew-status'), error.message, true);
    }
  }

  async function recordWaterReference() {
    const deviceId = $('#brew-device-select').value;
    if (!deviceId) {
      setMessage($('#brew-status'), 'Select an iSpindel first.', true);
      return;
    }
    if (!window.confirm(`Record the latest persisted sample from ${deviceId} as a 1.000 SG water reference? This does not replace the full calibration curve.`)) return;
    try {
      const reference = await api(`/api/device/${encodeURIComponent(deviceId)}/water-reference`, {
        method: 'POST', body: JSON.stringify({ label: $('#water-reference-label').value || 'Water reference' }),
      });
      setMessage($('#brew-status'), `Water reference saved: ${formatNumber(reference.angle, 3)}° at ${formatNumber(reference.temperature_c, 2)}°C.`);
    } catch (error) {
      setMessage($('#brew-status'), error.message, true);
    }
  }

  function assistantContext(kind) {
    const deviceId = $('#brew-device-select')?.value || null;
    // P2/W4: the server's AssistantScope contract (app/assistant_pipeline.py)
    // does NOT accept a `surface` field. The earlier UI sent it as a debug
    // breadcrumb; FastAPI rejects the request with 422 extra_forbidden and
    // the audit/rewrite POST never reaches the pipeline. Drop it here so
    // the page-driven flow lands in the same contract as direct API calls.
    return {
      recipe_id: state.selectedRecipe?.id || null,
      recipe_revision: state.selectedRecipe?.revision || null,
      brew_run_id: state.selectedBrew?.id || null,
      device_id: deviceId,
    };
  }

  function appendChat(container, role, text) {
    const message = node('div', `chat-message role-${role}`);
    message.append(node('strong', '', role === 'user' ? 'You' : 'Brewing assistant'));
    message.append(node('p', '', text));
    container.append(message);
    container.scrollTop = container.scrollHeight;
  }

  function setAssistantBusy(panel, busy) {
    const controls = $$('[data-assistant-action], [data-assistant-send]', panel);
    panel.dataset.assistantBusy = busy ? 'true' : 'false';
    controls.forEach((control) => {
      if (busy) {
        control.dataset.assistantWasDisabled = control.disabled ? 'true' : 'false';
        control.disabled = true;
      } else if (control.dataset.assistantWasDisabled !== undefined) {
        control.disabled = control.dataset.assistantWasDisabled === 'true' || !state.assistantAvailable;
        delete control.dataset.assistantWasDisabled;
      }
    });
  }

  async function sendAssistant(panel) {
    const input = $('[data-assistant-input]', panel);
    const log = $('[data-assistant-log]', panel);
    const status = $('[data-assistant-status]', panel);
    const text = input.value.trim();
    if (!text || panel.dataset.assistantBusy === 'true') return;
    appendChat(log, 'user', text);
    input.value = '';
    setMessage(status, 'Asking phone ZeroClaw…');
    try {
      const request = {
        message: text,
        context: assistantContext(panel.dataset.assistantKind),
        conversation_id: panel.dataset.conversationId || null,
        research: $('[data-assistant-research]', panel).checked,
      };
      if (panel.dataset.assistantKind === 'recipe') request.draft = recipeDraftForAssistant();
      setAssistantBusy(panel, true);
      const response = await api('/api/assistant/chat', {
        method: 'POST',
        body: JSON.stringify(request),
      });
      if (response.conversation_id) panel.dataset.conversationId = response.conversation_id;
      appendChat(log, 'assistant', response.message);
      const sources = Object.entries(response.research_status || {}).map(([name, value]) => `${name}: ${value}`);
      setMessage(status, sources.length ? `Ready · ${sources.join(' · ')}` : 'Ready');
    } catch (error) {
      appendChat(log, 'assistant', `Assistant unavailable: ${error.message}`);
      setMessage(status, 'ZeroClaw is not yet connected on this host.', true);
    } finally {
      setAssistantBusy(panel, false);
    }
  }

  function ensureAssistantActions(panel) {
    if ($('[data-assistant-actions]', panel)) return;
    const actions = node('div', 'assistant-actions');
    actions.dataset.assistantActions = 'true';
    const kind = panel.dataset.assistantKind;
    const buttons = kind === 'recipe'
      ? [['recipe_autofill', 'Autofill recipe', 'ordinary'], ['recipe_audit', 'Audit recipe', 'ordinary'], ['recipe_audit', 'Full audit', 'full']]
      : [['brew_analyze', 'Analyze brew', 'ordinary'], ['brew_event_draft', 'Draft next addition', 'ordinary']];
    buttons.forEach(([jobKind, label, auditProfile]) => {
      const button = node('button', 'secondary assistant-action', label);
      button.type = 'button';
      button.dataset.assistantAction = jobKind;
      button.dataset.assistantAuditProfile = auditProfile;
      actions.append(button);
    });
    const viewContext = node('button', 'secondary assistant-view-context', 'View context');
    viewContext.type = 'button';
    viewContext.disabled = true;
    viewContext.dataset.assistantViewContext = 'true';
    viewContext.addEventListener('click', () => viewAssistantContext(panel));
    actions.append(viewContext);
    const input = $('[data-assistant-input]', panel);
    input.parentElement.insertBefore(actions, input);
    const review = node('div', 'assistant-review');
    review.dataset.assistantReview = 'true';
    review.setAttribute('aria-live', 'polite');
    panel.append(review);
    actions.querySelectorAll('[data-assistant-action]').forEach((button) => {
      button.addEventListener('click', () => submitAssistantJob(panel, button.dataset.assistantAction, button.dataset.assistantAuditProfile));
    });
  }

  async function viewAssistantContext(panel) {
    const jobId = panel.dataset.assistantJobId;
    if (!jobId) return;
    try {
      const job = await api(`/api/assistant/jobs/${jobId}?include=context`);
      const context = job.context || {};
      const safe = {
        context_hash: job.context_hash,
        draft_hash: job.draft_hash,
        scope: job.scope,
        research_status: job.research_status,
        stages: job.stages,
        model: job.model,
        model_calls: (job.model_calls || []).map((call) => ({
          stage: call.stage, status: call.status, started_at: call.started_at,
          finished_at: call.finished_at, latency_ms: call.latency_ms, model: call.model,
        })),
        tool_calls: (job.tool_calls || []).map((call) => ({
          name: call.name, status: call.status, duration_ms: call.duration_ms,
        })),
        persisted_context_sections: Object.keys(context).sort(),
      };
      const review = $('[data-assistant-review]', panel);
      const details = node('details', 'assistant-context-receipt');
      details.open = true;
      details.append(node('summary', '', 'Persisted redacted context receipt'));
      details.append(node('pre', '', JSON.stringify(safe, null, 2)));
      review.replaceChildren(details);
      renderResearchEvidenceReview(panel, context.research_evidence_links || []);
    } catch (error) {
      setMessage($('[data-assistant-status]', panel), error.message, true);
    }
  }

  function renderResearchEvidenceReview(panel, evidenceLinks) {
    if (!Array.isArray(evidenceLinks) || !evidenceLinks.length) return;
    const review = $('[data-assistant-review]', panel);
    if (!review) return;
    const status = $('[data-assistant-status]', panel);
    const section = node('section', 'assistant-research-review');
    section.append(node('h4', '', 'Research evidence review'));
    const intro = node(
      'p',
      'field-help',
      'Mark each frozen citation as supports, contradicts, not_supporting, or unreviewed. The link keeps the version it pointed to when it was created.',
    );
    section.append(intro);
    const list = node('ul', 'assistant-research-review-list');
    evidenceLinks.forEach((link) => {
      const item = node('li', 'assistant-research-review-item');
      item.dataset.assistantResearchLink = String(link.link_id);
      item.dataset.assistantResearchVersion = String(link.version_id);
      const heading = node(
        'div',
        'assistant-research-review-heading',
        `${link.source_kind || 'source'}: ${link.title || link.source_url || 'unknown title'}`,
      );
      item.append(heading);
      const meta = node(
        'div',
        'assistant-research-review-meta',
        `version_no=${link.version_no ?? '?'} · support_status=${link.support_status || 'unreviewed'}`,
      );
      item.append(meta);
      if (link.excerpt) {
        item.append(node('blockquote', 'assistant-research-review-excerpt', link.excerpt));
      }
      const qualityMeta = node(
        'div',
        'assistant-research-review-quality-meta',
        `quality_status=${link.quality_status || 'unreviewed'}`,
      );
      item.append(qualityMeta);
      const qualityControls = node('div', 'assistant-research-review-controls');
      ['usable', 'rejected', 'unreviewed'].forEach((value) => {
        const button = node('button', 'secondary', `quality: ${value}`);
        button.type = 'button';
        button.dataset.assistantResearchQualityAction = value;
        button.setAttribute(
          'aria-label',
          `Mark version ${link.version_id} as ${value}`,
        );
        button.addEventListener('click', () => submitResearchQualityStatus(panel, link, value, status, qualityMeta));
        qualityControls.append(button);
      });
      item.append(qualityControls);
      const controls = node('div', 'assistant-research-review-controls');
      ['supports', 'contradicts', 'not_supporting', 'unreviewed'].forEach((value) => {
        const button = node('button', 'secondary', value);
        button.type = 'button';
        button.dataset.assistantResearchAction = value;
        button.setAttribute('aria-label', `Mark link ${link.link_id} as ${value}`);
        button.addEventListener('click', () => submitResearchSupportStatus(panel, link, value, status));
        controls.append(button);
      });
      item.append(controls);
      list.append(item);
    });
    section.append(list);
    review.append(section);
  }

  async function submitResearchSupportStatus(panel, link, supportStatus, status) {
    if (!link || link.link_id === undefined || link.link_id === null) return;
    const buttonSelector = `[data-assistant-research-action="${supportStatus}"]`;
    const item = $(`[data-assistant-research-link="${link.link_id}"]`, panel);
    if (item) {
      item.querySelectorAll('button[data-assistant-research-action]').forEach((btn) => {
        btn.disabled = true;
      });
    }
    try {
      await api(`/api/assistant/research/links/${link.link_id}/support-status`, {
        method: 'POST',
        body: JSON.stringify({ support_status: supportStatus }),
      });
      if (status) {
        setMessage(status, `Link ${link.link_id} marked ${supportStatus}.`, false);
      }
      const meta = item ? $('[data-assistant-research-link="' + link.link_id + '"] .assistant-research-review-meta', panel) : null;
      if (meta) {
        meta.textContent = `version_no=${link.version_no ?? '?'} · support_status=${supportStatus}`;
      }
    } catch (error) {
      if (status) setMessage(status, error.message, true);
    } finally {
      if (item) {
        item.querySelectorAll('button[data-assistant-research-action]').forEach((btn) => {
          btn.disabled = false;
        });
      }
    }
  }

  async function submitResearchQualityStatus(panel, link, qualityStatus, status, qualityMeta) {
    if (!link || link.version_id === undefined || link.version_id === null) return;
    const item = $(`[data-assistant-research-link="${link.link_id}"]`, panel);
    if (item) {
      item.querySelectorAll('button[data-assistant-research-quality-action]').forEach((btn) => {
        btn.disabled = true;
      });
    }
    try {
      await api(`/api/assistant/research/versions/${link.version_id}/quality-status`, {
        method: 'POST',
        body: JSON.stringify({ quality_status: qualityStatus }),
      });
      if (status) {
        setMessage(status, `Version ${link.version_id} marked ${qualityStatus}.`, false);
      }
      if (qualityMeta) {
        qualityMeta.textContent = `quality_status=${qualityStatus}`;
      }
    } catch (error) {
      if (status) setMessage(status, error.message, true);
    } finally {
      if (item) {
        item.querySelectorAll('button[data-assistant-research-quality-action]').forEach((btn) => {
          btn.disabled = false;
        });
      }
    }
  }

  function pointerTokens(pointer) {
    if (!pointer || !pointer.startsWith('/')) return [];
    return pointer.slice(1).split('/').map((token) => token.replaceAll('~1', '/').replaceAll('~0', '~'));
  }

  function stableListItem(list, token) {
    return list.find((item) => item && typeof item === 'object' && [
      item.ingredient_key, item.culture_key, item.addition_key, item.step_key,
    ].includes(token));
  }

  function readDiffValue(document, pointer) {
    const tokens = pointerTokens(pointer);
    if (!tokens.length) return { found: false, value: undefined };
    let current = document;
    for (const token of tokens) {
      if (Array.isArray(current)) {
        current = token.match(/^\d+$/) ? current[Number(token)] : stableListItem(current, token);
      } else if (current && typeof current === 'object' && Object.prototype.hasOwnProperty.call(current, token)) {
        current = current[token];
      } else {
        return { found: false, value: undefined };
      }
      if (current === undefined) return { found: false, value: undefined };
    }
    return { found: true, value: current };
  }

  function diffValuesEqual(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function applyDiffValue(document, pointer, value) {
    const tokens = pointerTokens(pointer);
    if (!tokens.length) return false;
    let current = document;
    for (let index = 0; index < tokens.length - 1; index += 1) {
      const token = tokens[index];
      if (Array.isArray(current)) {
        current = token.match(/^\d+$/) ? current[Number(token)] : stableListItem(current, token);
      } else if (current && typeof current === 'object') {
        current = current[token];
      } else {
        return false;
      }
      if (!current) return false;
    }
    const last = tokens[tokens.length - 1];
    if (Array.isArray(current)) {
      if (last.match(/^\d+$/)) {
        const position = Number(last);
        if (position < 0 || position >= current.length) return false;
        current[position] = value;
        return true;
      }
      const found = stableListItem(current, last);
      if (found) {
        const position = current.indexOf(found);
        if (tokens.length === 1 || (value && typeof value === 'object')) {
          current[position] = value;
          return true;
        }
        return false;
      }
      if (value && typeof value === 'object') {
        current.push(value);
        return true;
      }
      return false;
    }
    if (!current || typeof current !== 'object') return false;
    current[last] = value;
    return true;
  }

  // Capture the exact pre-apply draft so a one-shot Undo action can restore
  // every editable field without touching the saved revision. The snapshot is
  // stored on the assistant panel's dataset (in-memory only) and replaced on
  // the next successful apply so each Undo always targets the immediately
  // preceding draft.
  function captureRecipeDraftSnapshot() {
    let payload;
    try {
      payload = recipePayload();
    } catch (_) {
      // If the draft is currently invalid we cannot capture a clean snapshot;
      // the apply path will surface the same error.
      payload = null;
    }
    return {
      recipeId: $('#recipe-id').value,
      recipeRevision: $('#recipe-revision').value,
      recipeName: $('#recipe-name').value,
      recipeStyle: $('#recipe-style').value,
      recipeDescription: $('#recipe-description').value,
      recipeBaseVolume: $('#recipe-base-volume').value,
      recipeBeverageType: $('#recipe-beverage-type').value,
      recipeInitialVolume: $('#recipe-initial-volume').value,
      recipeTargetAbv: $('#recipe-target-abv').value,
      recipeTargetPh: $('#recipe-target-ph').value,
      recipeTargetSweetness: $('#recipe-target-sweetness').value,
      recipeNotes: $('#recipe-notes').value,
      payload: payload ? JSON.parse(JSON.stringify(payload)) : null,
      structured: JSON.parse(JSON.stringify(state.recipeStructured || {})),
    };
  }

  function restoreRecipeDraftSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return false;
    $('#recipe-id').value = snapshot.recipeId ?? '';
    $('#recipe-revision').value = snapshot.recipeRevision ?? '';
    $('#recipe-name').value = snapshot.recipeName ?? '';
    $('#recipe-style').value = snapshot.recipeStyle ?? '';
    $('#recipe-description').value = snapshot.recipeDescription ?? '';
    $('#recipe-base-volume').value = snapshot.recipeBaseVolume ?? '';
    $('#recipe-beverage-type').value = snapshot.recipeBeverageType ?? '';
    $('#recipe-initial-volume').value = snapshot.recipeInitialVolume ?? '';
    $('#recipe-target-abv').value = snapshot.recipeTargetAbv ?? '';
    $('#recipe-target-ph').value = snapshot.recipeTargetPh ?? '';
    $('#recipe-target-sweetness').value = snapshot.recipeTargetSweetness ?? 'unknown';
    $('#recipe-notes').value = snapshot.recipeNotes ?? '';
    state.recipeStructured = snapshot.structured
      ? JSON.parse(JSON.stringify(snapshot.structured))
      : {
          beverage_type: null,
          initial_fermenter_volume_l: null,
          target_metrics: {},
          culture_profiles: [],
          scheduled_additions: [],
          process_steps: [],
        };
    if (Array.isArray(snapshot.payload?.ingredients)) {
      $('#recipe-ingredients').replaceChildren(...snapshot.payload.ingredients.map(ingredientRow));
    } else {
      $('#recipe-ingredients').replaceChildren(ingredientRow());
    }
    renderStructuredEditors();
    $('#recipe-form').dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  }

  // Normalize a recipe_id / recipe_revision value to an explicit null-or-number
  // sentinel. `0` is treated as null because saved recipes are 1+ and the form
  // inputs serialise an empty field to '' or 0 — both must NOT match a scope
  // bound to a real saved recipe. Returning a single sentinel removes the
  // `!= null` short-circuit that previously let a null scope silently match a
  // newly-loaded saved recipe (cross-recipe apply binding defect).
  function normalizeScopeRecipeId(value) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : null;
  }

  // W4/F2: pollAssistantJob is recursive and continues to fire window.setTimeout
  // calls after every GET. If the operator selects a different recipe (which
  // calls clearRecipeForm / fillRecipeForm and rotates the recipe panel
  // binding) or submits a new assistant job (which rebinds assistantJobId),
  // the in-flight recursion must not survive. We pass a generation token
  // alongside the polled job id and refuse to render or mutate anything when
  // either no longer matches. The token is a monotonically increasing string
  // persisted on the panel's dataset so it survives across recursion frames
  // and is naturally short-lived (selection + new submit both rotate it).
  function nextAssistantBindingToken(panel) {
    const previous = Number(panel?.dataset?.assistantBindingToken || 0);
    const next = (Number.isFinite(previous) ? previous : 0) + 1;
    if (panel) panel.dataset.assistantBindingToken = String(next);
    return next;
  }

  function currentAssistantBindingToken(panel) {
    if (!panel) return 0;
    const value = Number(panel.dataset.assistantBindingToken || 0);
    return Number.isFinite(value) ? value : 0;
  }

  // Single-shot invalidator used by recipe selection / clearRecipeForm. Drops
  // every assistant binding on the recipe assistant panel: the apply
  // snapshot, applied-job id, live job id, audit job id, approved finding
  // ids, audit scope recipe id / revision, watchdog, AND rotates the
  // binding generation so any in-flight poll recursion becomes a no-op.
  // Unlike the previous single-key clearing, this guarantees that no
  // approval lineage keys (assistantAuditJobId, assistantApprovedFindingIds,
  // assistantAuditScopeRecipeId, assistantAuditScopeRecipeRevision,
  // assistantWatchdogMs) survive selection.
  function invalidateAssistantRecipeBinding(panel) {
    if (!panel) return;
    const lineageKeys = [
      'assistantPreApplyDraft',
      'assistantAppliedJobId',
      'assistantJobId',
      'assistantAuditJobId',
      'assistantApprovedFindingIds',
      'assistantAuditScopeRecipeId',
      'assistantAuditScopeRecipeRevision',
      'assistantWatchdogMs',
    ];
    lineageKeys.forEach((key) => { delete panel.dataset[key]; });
    nextAssistantBindingToken(panel);
  }

  // W4/F2: defensive render guard. renderAssistantJob unconditionally
  // rebinds panel.dataset.assistantJobId at the top of every call. If the
  // panel already has an assistantJobId set to a different job AND the live
  // binding still reflects that job (no selection / new submit has
  // rotated it), the new render would overwrite the live binding. Only
  // re-render when (a) no job is currently bound, (b) the polled job
  // matches the currently-bound job, or (c) the polled job IS the newest
  // job we just submitted (the immediate-binding site that happens before
  // the first poll). The token check is intentionally NOT included here:
  // this guard fires from inside pollAssistantJob / submitAssistantJob
  // AFTER the token has already been validated for that specific poll.
  function canRebindAssistantPanel(panel, polledJobId) {
    if (!panel) return false;
    const currentJobId = panel.dataset.assistantJobId || '';
    if (!currentJobId) return true;
    return currentJobId === polledJobId;
  }

  function applyRecipeProposal(panel, job, result, selectedPaths) {
    const proposal = result?.proposal;
    const applicableDiff = result?.applicable_diff;
    const scope = job?.scope || {};
    const currentRecipeId = normalizeScopeRecipeId($('#recipe-id').value);
    const currentRevision = normalizeScopeRecipeId($('#recipe-revision').value);
    const scopeRecipeId = normalizeScopeRecipeId(scope.recipe_id);
    const scopeRevision = normalizeScopeRecipeId(scope.recipe_revision);
    if (!job?.job_id || panel.dataset.assistantJobId !== job.job_id
      || !['recipe_autofill', 'recipe_rewrite'].includes(job.kind)
      || result?.kind !== job.kind
      || !proposal || !Array.isArray(applicableDiff)
      || scopeRecipeId !== currentRecipeId
      || scopeRevision !== currentRevision) {
      setMessage($('#recipe-status'), 'This proposal has no valid current recipe binding or validated diff. Run the workflow again.', true);
      return;
    }
    if (applicableDiff.some((change) => !change || typeof change.path !== 'string'
      || !change.path.startsWith('/') || !Object.prototype.hasOwnProperty.call(change, 'after'))) {
      setMessage($('#recipe-status'), 'The proposal diff is invalid. No form changes were made.', true);
      return;
    }
    // Per-field opt-in: only the operator-ticked subset is applied. Unchecked
    // entries are silently skipped; the remaining set is the only payload we
    // touch the form with.
    const selectedSet = new Set(
      Array.isArray(selectedPaths)
        ? selectedPaths.filter((entry) => typeof entry === 'string' && entry.startsWith('/'))
        : [],
    );
    if (!selectedSet.size) {
      setMessage($('#recipe-status'), 'Select at least one diff entry to apply to the form.', true);
      return;
    }
    const chosen = applicableDiff.filter((change) => selectedSet.has(change.path));
    if (!chosen.length) {
      setMessage($('#recipe-status'), 'No matching diff entries are checked. Tick at least one entry to apply.', true);
      return;
    }
    if (chosen.length !== selectedSet.size) {
      setMessage($('#recipe-status'), 'One or more checked diff entries are not part of this proposal. Unticked entries were ignored.', true);
      return;
    }
    // Capture the exact pre-apply draft before we touch the form so the
    // single-shot Undo can restore it without saving or changing the saved
    // revision.
    const preApplySnapshot = captureRecipeDraftSnapshot();
    if (!preApplySnapshot.payload) {
      setMessage($('#recipe-status'), 'Could not capture the current recipe draft. Fix validation errors before applying.', true);
      return;
    }
    // Atomic apply: validate every pointer against the snapshot, then mutate
    // the snapshot in place. Any conflict or failed pointer aborts before the
    // form is touched.
    const merged = JSON.parse(JSON.stringify(preApplySnapshot.payload));
    const conflicts = chosen.filter((change) => {
      const current = readDiffValue(merged, change.path);
      return !(current.found
        ? diffValuesEqual(current.value, change.before)
        : change.before === null);
    });
    if (conflicts.length) {
      setMessage($('#recipe-status'), 'The form changed after this proposal was generated. Reload and run the workflow again.', true);
      return;
    }
    const failed = chosen.filter((change) => !applyDiffValue(merged, change.path, change.after));
    if (failed.length) {
      setMessage($('#recipe-status'), 'The proposal could not be applied completely. No form changes were made.', true);
      return;
    }
    const applied = merged;
    $('#recipe-name').value = applied.name || '';
    $('#recipe-style').value = applied.style || '';
    $('#recipe-description').value = applied.description || '';
    $('#recipe-base-volume').value = applied.base_volume_l || '';
    $('#recipe-beverage-type').value = applied.beverage_type || '';
    $('#recipe-initial-volume').value = applied.initial_fermenter_volume_l ?? '';
    $('#recipe-target-abv').value = applied.target_metrics?.abv_percent ?? '';
    $('#recipe-target-ph').value = applied.target_metrics?.ph_min ?? '';
    $('#recipe-target-sweetness').value = applied.target_metrics?.sweetness || 'unknown';
    $('#recipe-notes').value = applied.notes || '';
    state.recipeStructured = {
      beverage_type: applied.beverage_type || null,
      initial_fermenter_volume_l: applied.initial_fermenter_volume_l ?? null,
      target_metrics: applied.target_metrics || {},
      culture_profiles: applied.culture_profiles || [],
      scheduled_additions: applied.scheduled_additions || [],
      process_steps: applied.process_steps || [],
    };
    $('#recipe-ingredients').replaceChildren(...(applied.ingredients || []).map(ingredientRow));
    renderStructuredEditors();
    $('#recipe-form').dispatchEvent(new Event('input', { bubbles: true }));
    // Record the exact pre-apply draft on the panel so the single-shot Undo
    // action can restore it. A second apply replaces the snapshot.
    panel.dataset.assistantPreApplyDraft = JSON.stringify(preApplySnapshot);
    panel.dataset.assistantAppliedJobId = job.job_id;
    // P2/W4: persist the bounded workflow binding so a reload can refetch
    // the authoritative job via GET without re-running the model.
    persistWorkflowSnapshot(panel);
    setMessage($('#recipe-status'), `Applied ${chosen.length} of ${applicableDiff.length} diff entr${chosen.length === 1 ? 'y' : 'ies'} to the form only. Review it, then explicitly save the recipe.`);
  }

  function undoAppliedRecipeProposal(panel) {
    const snapshotRaw = panel.dataset.assistantPreApplyDraft;
    if (!snapshotRaw) {
      setMessage($('#recipe-status'), 'No applied proposal is recorded on this panel to undo.', true);
      return false;
    }
    let snapshot;
    try {
      snapshot = JSON.parse(snapshotRaw);
    } catch (_) {
      setMessage($('#recipe-status'), 'The recorded pre-apply snapshot is unreadable. Reload the recipe to continue.', true);
      return false;
    }
    // Fail-closed recipe binding: the snapshot was captured for a specific
    // job and recipeId/recipeRevision. If the panel was re-rendered for a
    // different job, or the operator switched the live form to a different
    // recipe/revision, the snapshot no longer represents the live draft and
    // restoring it would silently clobber unrelated work. Surface an error,
    // leave the form and the recorded snapshot untouched, and refuse to
    // undo. The snapshot is not deleted so the operator can inspect or
    // discard it explicitly.
    const boundJobId = panel.dataset.assistantAppliedJobId;
    const panelJobId = panel.dataset.assistantJobId;
    if (!boundJobId || !panelJobId || boundJobId !== panelJobId) {
      setMessage($('#recipe-status'), 'The recorded pre-apply snapshot belongs to a different assistant job. Re-open the review before undoing.', true);
      return false;
    }
    const liveRecipeId = $('#recipe-id').value;
    const liveRecipeRevision = $('#recipe-revision').value;
    if (String(snapshot.recipeId ?? '') !== String(liveRecipeId ?? '')
      || String(snapshot.recipeRevision ?? '') !== String(liveRecipeRevision ?? '')) {
      setMessage($('#recipe-status'), 'The current recipe changed since the proposal was applied. Reload the recipe before undoing.', true);
      return false;
    }
    // Restore the exact pre-apply draft without saving and without touching
    // the saved recipe revision.
    const restored = restoreRecipeDraftSnapshot(snapshot);
    if (!restored) return false;
    delete panel.dataset.assistantPreApplyDraft;
    delete panel.dataset.assistantAppliedJobId;
    setMessage($('#recipe-status'), 'Restored the exact pre-apply draft. No save was issued; the saved revision is unchanged.');
    return true;
  }

  // Render one unchecked checkbox per applicable_diff entry with a concise
  // before/after preview. Each checkbox is bound to a single JSON pointer so
  // apply can run the conflict + pointer checks against exactly the ticked
  // subset.
  function renderApplicableDiffChoices(panel, job, result, onApply) {
    const applicableDiff = Array.isArray(result?.applicable_diff) ? result.applicable_diff : [];
    const validEntries = applicableDiff.filter((change) => change
      && typeof change.path === 'string'
      && change.path.startsWith('/')
      && Object.prototype.hasOwnProperty.call(change, 'after'));
    const wrapper = node('div', 'assistant-diff-choices');
    wrapper.append(node('strong', '', `Pick the diff entries to apply (${validEntries.length} available)`));
    if (!validEntries.length) {
      wrapper.append(node('p', 'empty-copy', 'This proposal has no applicable diff entries.'));
      return wrapper;
    }
    const list = node('ul', 'assistant-diff-list');
    validEntries.forEach((change, index) => {
      const item = node('li', 'assistant-diff-entry');
      const label = node('label', 'diff-row');
      const checkbox = node('input');
      checkbox.type = 'checkbox';
      checkbox.checked = false;
      // Per-field / per-pointer identity: the path itself is the unique key.
      // We carry the job_id so apply can prove the entry belongs to the
      // current result and not a stale panel closure.
      checkbox.dataset.diffPath = change.path;
      checkbox.dataset.diffJobId = job?.job_id || '';
      checkbox.dataset.diffIndex = String(index);
      checkbox.setAttribute('aria-label', `Apply diff at ${change.path}`);
      label.append(checkbox);
      label.append(node('code', 'diff-path', change.path));
      label.append(node('span', 'diff-preview', `${previewDiffValue(change.before)} → ${previewDiffValue(change.after)}`));
      item.append(label);
      list.append(item);
    });
    wrapper.append(list);
    const apply = node('button', 'secondary', 'Apply checked entries to form (not saved)');
    apply.type = 'button';
    apply.dataset.assistantAction = 'apply';
    apply.addEventListener('click', () => {
      const tickedPaths = $$('[data-diff-path]:checked', wrapper).map((input) => input.dataset.diffPath);
      onApply(tickedPaths);
    });
    wrapper.append(apply);
    return wrapper;
  }

  // Concise JSON preview for a single before/after value. Nested objects are
  // summarised in place so the operator can scan each entry without leaving
  // the review panel.
  function previewDiffValue(value) {
    if (value === null || value === undefined) return '∅';
    const text = JSON.stringify(value);
    if (text.length <= 80) return text;
    return `${text.slice(0, 77)}…`;
  }

  async function recordDraftedEvent(job, result) {
    const brewId = result.brew_run_id || state.selectedBrew?.id;
    if (!brewId || !result.addition_key) return;
    if (!window.confirm(`Record the proposed addition ${result.addition_key} on brew #${brewId}?`)) return;
    try {
      await api(`/api/brews/${brewId}/events`, {
        method: 'POST',
        body: JSON.stringify({
          event_type: result.event_type === 'scheduled_addition_skipped' ? 'scheduled_addition_skipped' : 'scheduled_addition_recorded',
          source: 'agent',
          client_request_id: newClientId(),
          data: result.event_type === 'scheduled_addition_skipped'
            ? { addition_key: result.addition_key, reason: result.reason || 'Operator skipped' }
            : { addition_key: result.addition_key, actual_quantity: result.quantity, actual_unit: result.unit },
        }),
      });
      setMessage($('#brew-status'), `Drafted addition ${result.addition_key} recorded.`);
      await loadDevicesAndBrews();
    } catch (error) {
      setMessage($('#brew-status'), error.message, true);
    }
  }

  function renderAssistantJob(panel, job) {
    // W4/F2: defensive render guard. renderAssistantJob rebinds
    // panel.dataset.assistantJobId at the top of every call. If the panel
    // already has an assistantJobId set to a different job AND the live
    // binding still reflects that job (no selection / new submit has
    // rotated it), the new render would overwrite the live binding with a
    // stale job. Only render when (a) no job is currently bound, or (b)
    // the polled job matches the currently-bound job. The token check is
    // intentionally NOT done here: this function is invoked from
    // pollAssistantJob AFTER the token has already been validated for
    // that specific poll, and from submitAssistantJob's immediate-binding
    // site for the just-submitted job id (so the rebind is the desired
    // direction). The pollAssistantJob pre-/post-await guards remain the
    // authoritative defense; this guard is an extra fail-closed check.
    if (!canRebindAssistantPanel(panel, job?.job_id)) {
      return;
    }
    panel.dataset.assistantJobId = job.job_id;
    const viewContext = $('[data-assistant-view-context]', panel);
    if (viewContext) viewContext.disabled = false;
    const review = $('[data-assistant-review]', panel);
    review.replaceChildren();
    if (Array.isArray(job.stages) && job.stages.length) {
      const stages = node('ol', 'assistant-stage-list');
      job.stages.forEach((stage) => {
        const item = node('li', `assistant-stage stage-${stage.status || 'pending'}`);
        item.append(node('strong', '', stage.stage || 'stage'));
        item.append(node('span', '', stage.status || 'pending'));
        if (stage.detail && stage.detail.reason) item.append(node('small', '', stage.detail.reason));
        stages.append(item);
      });
      review.append(stages);
    }
    if (job.status === 'failed') {
      review.append(node('p', 'status is-error', `Job failed: ${job.error_code || 'assistant_error'}`));
      if (job.parse_errors?.length) review.append(node('small', '', 'The model response was rejected by the workflow contract.'));
      return;
    }
    const result = job.result || {};
    review.append(node('strong', '', `${result.kind || job.kind} result — review before applying`));
    if (result.summary) review.append(node('p', '', result.summary));
    if (Array.isArray(result.findings)) {
      const findings = node('div', 'assistant-findings');
      result.findings.forEach((finding) => {
        const label = node('label', 'finding-row');
        const checkbox = node('input');
        checkbox.type = 'checkbox';
        checkbox.checked = false;
        checkbox.dataset.findingId = finding.finding_id;
        label.append(checkbox, node('span', '', `${finding.severity || 'finding'} · ${finding.field_path || 'recipe'} · ${finding.rationale || ''}`));
        findings.append(label);
      });
      review.append(findings);
      const approve = node('button', 'secondary', 'Approve selected findings for rewrite');
      approve.type = 'button';
      approve.disabled = !result.findings.length;
      approve.addEventListener('click', async () => {
        const status = $('[data-assistant-status]', panel);
        // Pre-POST guard: the panel must still be bound to this audit job.
        // A re-render (e.g. recipe selection or new poll) can have replaced
        // assistantJobId under our closure; refuse the POST instead of
        // allowing approval to be recorded against the wrong job.
        if (panel.dataset.assistantJobId !== job.job_id) {
          setMessage(status, 'This panel is bound to a different audit. Re-open the review to approve.', true);
          return;
        }
        const findingIds = $$('[data-finding-id]:checked', findings).map((input) => input.dataset.findingId);
        let approvedRecord;
        try {
          approvedRecord = await api(`/api/assistant/jobs/${job.job_id}/approve`, {
            method: 'POST',
            body: JSON.stringify({ decision: findingIds.length === result.findings.length ? 'approved' : 'partial', finding_ids: findingIds }),
          });
        } catch (error) {
          setMessage(status, error.message, true);
          return;
        }
        // Post-await guard: the panel must STILL be bound to this audit job
        // before we persist the approval binding. A concurrent re-render or
        // poll between the POST and now can have swapped assistantJobId;
        // writing the approval binding under a stale closure would overwrite
        // the live job's binding with the previous job's approval.
        if (panel.dataset.assistantJobId !== job.job_id) {
          setMessage(status, 'The audit binding changed while the approval was in flight. Re-open the review to approve.', true);
          return;
        }
        // Persist the approval binding on the current panel closure so the
        // follow-up rewrite button can re-validate scope, job id, and
        // selected finding IDs before POSTing /api/assistant/jobs. The scope
        // values are stored as empty-string for null/undefined and as the
        // canonical string of the normalised positive number otherwise, so
        // the click handler can re-validate through normalizeScopeRecipeId
        // without a truthy-empty bypass.
        panel.dataset.assistantAuditJobId = job.job_id;
        panel.dataset.assistantApprovedFindingIds = JSON.stringify(Array.isArray(approvedRecord?.approved_finding_ids) ? approvedRecord.approved_finding_ids : findingIds);
        const scopeRecord = approvedRecord?.scope || job.scope || {};
        const approvedRecipeId = normalizeScopeRecipeId(scopeRecord.recipe_id);
        const approvedRecipeRevision = normalizeScopeRecipeId(scopeRecord.recipe_revision);
        panel.dataset.assistantAuditScopeRecipeId = approvedRecipeId != null ? String(approvedRecipeId) : '';
        panel.dataset.assistantAuditScopeRecipeRevision = approvedRecipeRevision != null ? String(approvedRecipeRevision) : '';
        if (findingIds.length && (approvedRecord?.approval_decision === 'approved' || approvedRecord?.approval_decision === 'partial')) {
          rewriteButton.disabled = false;
          rewriteButton.title = 'Submit a reviewed recipe_rewrite using the approved audit and current draft.';
        }
        // P2/W4: persist the bounded audit lineage so a reload can refetch
        // the audit job and re-bind the approved-finding set without
        // re-running the approve API call.
        persistWorkflowSnapshot(panel);
        setMessage(status, 'Audit approval recorded. A rewrite remains a separate reviewed job.');
      });
      review.append(approve);
    }
    // Only surface the rewrite action for a succeeded recipe_audit with a
    // recorded approval on this panel; every other state stays inert. The
    // button node itself is still constructed so the approve click handler
    // closure can flip its disabled flag, but it is only appended to the
    // DOM for the eligible audit path. Other job kinds (recipe_autofill,
    // recipe_rewrite, brew_analyze, brew_event_draft) and pending/running/
    // failed jobs never expose the rewrite action in the review.
    const rewriteButton = node('button', 'secondary', 'Generate proposed changes');
    rewriteButton.type = 'button';
    rewriteButton.dataset.assistantAction = 'rewrite';
    rewriteButton.dataset.assistantRewrite = 'true';
    rewriteButton.setAttribute('aria-label', 'Generate proposed changes from the approved audit');
    // The rewrite action is gated on five explicit conditions. The zero-
    // findings guard ensures a succeeded recipe_audit with no findings never
    // surfaces the action, and the approved-finding-ids guard prevents the
    // approve handler from re-enabling a panel whose server-approved set is
    // empty (an empty approval must stay disabled, regardless of the
    // operator's local checkbox state).
    const recordedApprovedFindingIds = (() => {
      try {
        const parsed = JSON.parse(panel.dataset.assistantApprovedFindingIds || '[]');
        return Array.isArray(parsed) ? parsed : [];
      } catch (_) {
        return [];
      }
    })();
    const canRewriteNow = job.kind === 'recipe_audit'
      && job.status === 'succeeded'
      && Array.isArray(job.result?.findings)
      && job.result.findings.length > 0
      && recordedApprovedFindingIds.length > 0
      && (job.approval_decision === 'approved' || job.approval_decision === 'partial');
    rewriteButton.disabled = !canRewriteNow;
    rewriteButton.title = canRewriteNow
      ? 'Submit a reviewed recipe_rewrite using the approved audit and current draft.'
      : 'Approve the audit findings on this panel first.';
    rewriteButton.addEventListener('click', async () => {
      const status = $('[data-assistant-status]', panel);
      // W4/F2: double-click guard. A fast double-click on the rewrite
      // button must not enqueue two jobs against the same approved audit:
      // setAssistantBusy(panel, true) at the bottom of the handler flips
      // the dataset flag, but the second click can already have entered
      // the handler before that flip completes. Refuse early on the busy
      // flag and surface the same "assistant busy" pattern as the
      // existing pre/post race guards without breaking the approval pre/
      // post safeguards further down.
      if (panel.dataset.assistantBusy === 'true') {
        return;
      }
      // Stale-binding guard: the rewrite must be authored against the same
      // audit and panel binding that recorded the approval. Re-rendering or
      // re-approving invalidates the closure.
      if (panel.dataset.assistantJobId !== job.job_id) {
        setMessage(status, 'This panel is bound to a different audit. Re-open the review to continue.', true);
        return;
      }
      if (panel.dataset.assistantAuditJobId !== job.job_id) {
        setMessage(status, 'The audit approval is no longer recorded on this panel. Approve again to continue.', true);
        return;
      }
      let approvedFindingIds;
      try {
        approvedFindingIds = JSON.parse(panel.dataset.assistantApprovedFindingIds || '[]');
      } catch (_) {
        approvedFindingIds = [];
      }
      if (!Array.isArray(approvedFindingIds) || !approvedFindingIds.length) {
        setMessage(status, 'No approved findings are bound to this panel. Approve at least one finding first.', true);
        return;
      }
      const liveScope = assistantContext('recipe');
      // Audit-scope comparison: read both recipe_id and recipe_revision
      // through the same explicit null-or-number sentinel as the apply
      // pipeline. Comparing unconditionally (instead of truthy-skipping the
      // null-draft case) closes the loophole where a null audit scope would
      // silently match a freshly-loaded saved recipe — exactly the same
      // cross-recipe apply-binding defect that applyRecipeProposal guards
      // against. Mismatch on EITHER id or revision fails closed.
      const auditRecipeId = normalizeScopeRecipeId(panel.dataset.assistantAuditScopeRecipeId);
      const auditRecipeRevision = normalizeScopeRecipeId(panel.dataset.assistantAuditScopeRecipeRevision);
      if (auditRecipeId !== normalizeScopeRecipeId(liveScope.recipe_id)) {
        setMessage(status, 'The current recipe changed since the audit. Run a new audit before requesting a rewrite.', true);
        return;
      }
      if (auditRecipeRevision !== normalizeScopeRecipeId(liveScope.recipe_revision)) {
        setMessage(status, 'The current recipe revision changed since the audit. Run a new audit before requesting a rewrite.', true);
        return;
      }
      let draft;
      try {
        draft = recipeDraftForAssistant();
      } catch (error) {
        setMessage(status, `Could not capture the current recipe draft: ${error.message}`, true);
        return;
      }
      // No findings selected right now: the action is operator-initiated and
      // must require an explicit, current checkbox selection before firing.
      const liveSelection = $$('[data-finding-id]:checked', panel).map((input) => input.dataset.findingId);
      if (!liveSelection.length) {
        setMessage(status, 'Select at least one approved finding to include in the rewrite.', true);
        return;
      }
      // The exact checked IDs are the only IDs sent; we never widen back to
      // the recorded approval set, because that would silently resubmit
      // unchecked findings.
      const message = ($('[data-assistant-input]', panel)?.value || '').trim().slice(0, 4000);
      const rewriteRequest = {
        kind: 'recipe_rewrite',
        client_request_id: newClientId(),
        surface: 'recipe',
        message: message || 'Rewrite the current recipe using the approved audit findings.',
        scope: liveScope,
        research: false,
        audit_profile: 'ordinary',
        draft,
        parent_job_id: job.job_id,
        approved_finding_ids: liveSelection,
        // P2/W4: a review-driven rewrite must surface every approved
        // finding's intended change as an applicable_diff entry so the
        // operator can tick them on the form. The default empty_only fill
        // strategy silently drops non-empty fields (style name changes)
        // and would leave the operator with nothing to apply.
        fill_strategy: 'full',
      };
      setAssistantBusy(panel, true);
      setMessage(status, 'Queuing reviewed rewrite for ZeroClaw…');
      // W4/F2: rotate the binding generation before POST so any stale poll
      // for the prior approved-audit job becomes a no-op, then pass the new
      // token into the immediate-binding site + recursive poll path. The
      // same is-invalidator pattern as submitAssistantJob so the recursion
      // cannot rebind the old approved audit against the new rewrite job.
      const rewriteToken = nextAssistantBindingToken(panel);
      try {
        const submitted = await api('/api/assistant/jobs', {
          method: 'POST',
          body: JSON.stringify(rewriteRequest),
        });
        // W4/F2: immediate job binding site. Bind the panel to the new
        // rewrite job BEFORE kicking off the poll, and only when the token
        // has not rotated since the POST. A recipe selection / another
        // submit during the await must abort before any dataset write.
        if (currentAssistantBindingToken(panel) !== rewriteToken) {
          setMessage(status, 'The recipe selection changed while the rewrite was being submitted. Re-open the workflow.', true);
          setAssistantBusy(panel, false);
          return;
        }
        panel.dataset.assistantJobId = submitted.job_id;
        panel.dataset.assistantAuditJobId = job.job_id;
        panel.dataset.assistantApprovedFindingIds = JSON.stringify(approvedFindingIds);
        panel.dataset.assistantAuditScopeRecipeId = auditRecipeId != null ? String(auditRecipeId) : '';
        panel.dataset.assistantAuditScopeRecipeRevision = auditRecipeRevision != null ? String(auditRecipeRevision) : '';
        panel.dataset.assistantWatchdogMs = '210000';
        // P2/W4: persist the rewrite's queued job id so a reload resumes
        // polling the same authoritative job via GET without re-submitting.
        persistWorkflowSnapshot(panel);
        if (submitted.job_id) pollAssistantJob(panel, submitted.job_id, Date.now(), rewriteToken);
      } catch (error) {
        setMessage(status, error.message, true);
        setAssistantBusy(panel, false);
      }
    });
    // Append the rewrite button only for a succeeded recipe_audit job. The
    // node is constructed above so the approve-click closure can mutate its
    // disabled flag, but other job kinds and pending/running/failed jobs
    // never expose the action in the DOM.
    if (job.kind === 'recipe_audit' && job.status === 'succeeded') {
      review.append(rewriteButton);
    }
    if (result.proposal) {
      review.append(renderApplicableDiffChoices(panel, job, result, (selectedPaths) => {
        applyRecipeProposal(panel, job, result, selectedPaths);
        // Re-render the review so the Undo action appears next to the
        // choices without disturbing the audit/approval state above it.
        // Deduplicate any pre-existing Undo button before appending the
        // single-shot one so repeated applies / re-renders never surface a
        // stacked row of Undo actions bound to the same snapshot.
        const reviewAgain = $('[data-assistant-review]', panel);
        $$('[data-assistant-action="undo"]', reviewAgain).forEach((button) => button.remove());
        if (panel.dataset.assistantPreApplyDraft) {
          const undoButton = node('button', 'secondary', 'Undo applied proposal');
          undoButton.type = 'button';
          undoButton.dataset.assistantAction = 'undo';
          undoButton.addEventListener('click', () => undoAppliedRecipeProposal(panel));
          reviewAgain.append(undoButton);
        }
      }));
      // If a previous apply is still recorded on this panel, surface the
      // Undo action even before the operator re-opens the choice list.
      // Same dedup rule as above: only one Undo button is ever visible per
      // snapshot, regardless of how many times this render fires.
      if (panel.dataset.assistantPreApplyDraft && panel.dataset.assistantAppliedJobId === job.job_id) {
        $$('[data-assistant-action="undo"]', review).forEach((button) => button.remove());
        const undoButton = node('button', 'secondary', 'Undo applied proposal');
        undoButton.type = 'button';
        undoButton.dataset.assistantAction = 'undo';
        undoButton.addEventListener('click', () => undoAppliedRecipeProposal(panel));
        review.append(undoButton);
      }
    }
    if (result.kind === 'brew_event_draft' && result.addition_key) {
      const record = node('button', 'secondary', 'Record this draft explicitly');
      record.type = 'button';
      record.addEventListener('click', () => recordDraftedEvent(job, result));
      review.append(record);
    }
    const details = node('details', 'assistant-json');
    details.append(node('summary', '', 'Show validated result JSON'));
    details.append(node('pre', '', JSON.stringify(result, null, 2)));
    review.append(details);
  }

  async function pollAssistantJob(panel, jobId, startedAt, expectedToken) {
    // W4/F2: refuse to render or mutate the panel if the binding generation
    // has rotated since this poll started. Selection / clearRecipeForm
    // (invalidateAssistantRecipeBinding) bump the token and delete
    // assistantJobId, so an in-flight poll whose awaited GET just resolved
    // must drop out before renderAssistantJob runs OR before the
    // setTimeout re-schedules itself. Also refuse if the polled job id is
    // no longer the live binding: a newer submit can re-bind assistantJobId
    // even though the token has not yet rotated.
    const tokenMatches = expectedToken === undefined
      || expectedToken === currentAssistantBindingToken(panel);
    const jobMatches = !panel?.dataset?.assistantJobId
      || panel.dataset.assistantJobId === jobId;
    if (!tokenMatches || !jobMatches) {
      return;
    }
    try {
      const job = await api(`/api/assistant/jobs/${jobId}`);
      // Post-await guards: re-check both the token AND the live job id
      // after the GET resolved. Without this, a recipe selection made
      // during the await would still let renderAssistantJob run against
      // the new panel state and rebind the old job.
      if ((expectedToken !== undefined && currentAssistantBindingToken(panel) !== expectedToken)
        || (panel?.dataset?.assistantJobId && panel.dataset.assistantJobId !== jobId)) {
        return;
      }
      if (job.status === 'succeeded' || job.status === 'failed') {
        if (!canRebindAssistantPanel(panel, jobId)) {
          setAssistantBusy(panel, false);
          return;
        }
        renderAssistantJob(panel, job);
        // P2/W4: persist the bounded workflow binding so a reload can
        // refetch the authoritative job via GET without re-running the
        // model or re-fetching research.
        persistWorkflowSnapshot(panel);
        setMessage($('[data-assistant-status]', panel), job.status === 'succeeded' ? 'Review ready.' : 'Assistant job failed.', job.status !== 'succeeded');
        setAssistantBusy(panel, false);
        return;
      }
      const watchdogMs = Number(panel.dataset.assistantWatchdogMs || 150000);
      if (Date.now() - startedAt > watchdogMs) {
        setMessage($('[data-assistant-status]', panel), 'Assistant job watchdog expired; check its receipt.', true);
        setAssistantBusy(panel, false);
        return;
      }
      window.setTimeout(() => pollAssistantJob(panel, jobId, startedAt, expectedToken), 500);
    } catch (error) {
      // A late-failing GET must still respect the binding guards: a token
      // mismatch during the await means we are no longer the live poller.
      if ((expectedToken !== undefined && currentAssistantBindingToken(panel) !== expectedToken)
        || (panel?.dataset?.assistantJobId && panel.dataset.assistantJobId !== jobId)) {
        return;
      }
      setMessage($('[data-assistant-status]', panel), error.message, true);
      setAssistantBusy(panel, false);
    }
  }

  async function submitAssistantJob(panel, kind, auditProfile = 'ordinary') {
    const status = $('[data-assistant-status]', panel);
    const text = $('[data-assistant-input]', panel).value.trim();
    const request = {
      kind,
      client_request_id: newClientId(),
      surface: panel.dataset.assistantKind,
      message: text || (kind === 'recipe_audit' ? 'Audit the current recipe.' : 'Review the current brew context.'),
      scope: assistantContext(panel.dataset.assistantKind),
      research: $('[data-assistant-research]', panel).checked,
      audit_profile: auditProfile,
    };
    if (panel.dataset.assistantKind === 'recipe') {
      try {
        request.draft = recipeDraftForAssistant();
      } catch (error) {
        setMessage(status, error.message, true);
        return;
      }
    }
    setAssistantBusy(panel, true);
    panel.dataset.assistantWatchdogMs = kind === 'recipe_audit' && auditProfile === 'full' ? '660000' : '150000';
    setMessage(status, 'Queued for phone ZeroClaw…');
    // Capture the binding token BEFORE the POST so the immediate-binding
    // site below can be validated against the same generation the poll
    // recursion will verify on every tick. submitAssistantJob rotates the
    // token (calling nextAssistantBindingToken) so any prior in-flight
    // poll for this panel becomes a no-op; the new poll will only ever
    // observe the new token.
    const submittedToken = nextAssistantBindingToken(panel);
    try {
      const receipt = await api('/api/assistant/jobs', { method: 'POST', body: JSON.stringify(request) });
      // Immediate job binding: bind the panel BEFORE kicking off polling.
      // The token MUST still match (no recipe selection / new submit
      // happened during the POST) and we deliberately allow the binding
      // through even when no prior assistantJobId exists on the panel.
      if (currentAssistantBindingToken(panel) !== submittedToken) {
        setMessage(status, 'The recipe selection changed while the job was being submitted. Re-open the workflow.', true);
        setAssistantBusy(panel, false);
        return;
      }
      panel.dataset.assistantJobId = receipt.job_id;
      $('[data-assistant-review]', panel).replaceChildren();
      // P2/W4: persist the freshly-bound workflow job id so a reload can
      // resume polling the same authoritative job via GET without re-submitting.
      persistWorkflowSnapshot(panel);
      pollAssistantJob(panel, receipt.job_id, Date.now(), submittedToken);
    } catch (error) {
      setMessage(status, error.message, true);
      setAssistantBusy(panel, false);
    }
  }

  function bindAssistantPanels() {
    $$('.assistant-panel').forEach((panel) => {
      ensureAssistantActions(panel);
      $('[data-assistant-send]', panel).addEventListener('click', () => sendAssistant(panel));
      $('[data-assistant-input]', panel).addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey) {
          event.preventDefault();
          sendAssistant(panel);
        }
      });
    });
  }

  async function loadAssistantStatus() {
    try {
      const status = await api('/api/assistant/status');
      state.assistantAvailable = status.available;
      $$('.assistant-panel').forEach((panel) => {
        $('.assistant-state', panel).textContent = status.available ? 'Agent ready' : (status.configured ? 'Agent offline' : 'Not configured');
        $('[data-assistant-send]', panel).disabled = !status.available;
        setMessage(
          $('[data-assistant-status]', panel),
          status.available ? 'Ready' : (status.configured ? 'Phone ZeroClaw is offline.' : 'Phone ZeroClaw is not configured.'),
          !status.available,
        );
      });
    } catch (error) {
      state.assistantAvailable = false;
      $$('.assistant-panel').forEach((panel) => {
        $('.assistant-state', panel).textContent = 'Status unavailable';
        $('[data-assistant-send]', panel).disabled = true;
        setMessage($('[data-assistant-status]', panel), error.message, true);
      });
    }
  }

  function bindControls() {
    $('#new-recipe').addEventListener('click', clearRecipeForm);
    $('#add-ingredient').addEventListener('click', () => {
      syncRecipeStructured();
      $('#recipe-ingredients').append(ingredientRow());
      renderStructuredEditors();
    });
    $('#add-culture-profile').addEventListener('click', () => {
      syncRecipeStructured();
      state.recipeStructured.culture_profiles.push({ culture_key: newClientId(), culture_kind: 'other', identity_assertion: 'unknown', organism_status: 'uncharacterized' });
      renderStructuredEditors();
    });
    $('#add-scheduled-addition').addEventListener('click', () => {
      syncRecipeStructured();
      state.recipeStructured.scheduled_additions.push({ addition_key: newClientId(), series_key: newClientId(), sequence: state.recipeStructured.scheduled_additions.length + 1, series_kind: 'manual', trigger: { kind: 'manual' }, unit: 'g' });
      renderStructuredEditors();
    });
    $('#add-process-step').addEventListener('click', () => {
      syncRecipeStructured();
      state.recipeStructured.process_steps.push({ step_key: newClientId(), sequence: state.recipeStructured.process_steps.length + 1, phase: 'other' });
      renderStructuredEditors();
    });
    $('#recipe-form').addEventListener('submit', saveRecipe);
    $('#recipe-target-volume').addEventListener('input', (event) => {
      $('#recipe-target-volume-number').value = event.target.value;
      scheduleScale();
    });
    $('#recipe-target-volume-number').addEventListener('change', (event) => {
      const value = Math.min(200, Math.max(1, Number(event.target.value) || 1));
      $('#recipe-target-volume').value = value;
      event.target.value = value;
      scheduleScale();
    });
    $('#brew-device-select').addEventListener('change', renderSelectedDevice);
    $('#save-operating-intent').addEventListener('click', saveOperatingIntent);
    $('#begin-brew').addEventListener('click', beginBrew);
    $('#stop-brew').addEventListener('click', stopBrew);
    $('#record-water-reference').addEventListener('click', recordWaterReference);
  }

  async function initialize() {
    bindTabs();
    bindControls();
    bindAssistantPanels();
    clearRecipeForm();
    try {
      await Promise.all([loadRecipes(), loadDevicesAndBrews(), loadAssistantStatus()]);
      appReady = true;
    } catch (error) {
      setMessage($('#brew-status'), `Brewing data unavailable: ${error.message}`, true);
      setMessage($('#recipe-status'), `Recipe data unavailable: ${error.message}`, true);
    }
    window.setInterval(() => {
      if (document.body.dataset.activeTab !== 'brew') return;
      loadDevicesAndBrews().catch((error) => {
        setMessage($('#brew-status'), `Could not refresh brew state: ${error.message}`, true);
      });
    }, 30000);
  }

  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/static/service-worker.js', { scope: '/' }).catch(() => {
        // The web UI remains fully functional when PWA installation is unavailable.
      });
    });
  }

  initialize();
})();
