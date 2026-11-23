/* 原生 JS：轮询 /api/state（默认 3s）并渲染三视图；无第三方依赖。 */
(function () {
  'use strict';

  var state = { plan: [], records: {}, logs: [], summary: {}, poll_sec: 3, status_color: false };
  var currentView = 'overview';
  // 顶层大章权威清单（与运行时 runtime.py 一致），仅用于表单展示与提交。
  var CHAPTERS = [
    { name: '0_预习', id: '20260907180845_0001' },
    { name: '1_绪论', id: '20260907180845_0002' },
    { name: '2_测序', id: '20260907180845_0005' },
    { name: '3_组装', id: '20260907180845_0008' },
    { name: '4_注释', id: '20260907180845_0017' },
    { name: '5_数据库', id: '20260907180845_0030' },
    { name: '6_实验', id: '20260907180845_0034' },
    { name: '9_复习', id: '20260907180845_0044' }
  ];
  var PHASE_TEXT = {
    not_started: '未启动', starting: '启动中', running: '执行中',
    completed: '已完成', failed: '失败'
  };
  var SOURCE_TEXT = { template: '模板默认', env: '环境变量', web: '网页修改' };
  var formPrefilled = false;  // 首帧预填一次，之后绝不覆盖用户编辑
  var startBusy = false;      // 提交期间防连点
  var STATUS = {
    pending: { icon: '…', text: '待复习' },
    running: { icon: '●', text: '进行中' },
    success: { icon: '✓', text: '已完成' },
    failed: { icon: '✕', text: '失败' },
    skipped: { icon: '—', text: '已跳过' }
  };

  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function statusHtml(value) {
    var status = STATUS[value] || STATUS.pending;
    return '<span class="status status-' + esc(value) + '">' + status.icon + ' ' + status.text + '</span>';
  }

  function bar(rate) {
    var n = Math.max(0, Math.min(10, Math.round((Number(rate) || 0) / 10)));
    return new Array(n + 1).join('█') + new Array(11 - n).join('░') + ' ' + (Number(rate) || 0).toFixed(1) + '%';
  }

  function recordFor(task) {
    return state.records[task.unique_key] || {};
  }

  function switchView(name) {
    currentView = name;
    var tabs = document.querySelectorAll('.tab');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].classList.toggle('active', tabs[i].getAttribute('data-view') === name);
    }
    ['overview', 'records', 'bank'].forEach(function (view) {
      var section = document.getElementById('view-' + view);
      if (section) { section.classList.toggle('hidden', view !== name); }
    });
    if (name === 'bank') { loadBank(); }
    if (name === 'records') { renderRecords(); }
  }

  function renderOverview() {
    var summary = state.summary || {};
    var metrics = [
      ['执行单元总数', summary.total || 0], ['已完成', summary.done || 0],
      ['平均分', summary.average_score || 0], ['满分数', summary.full_scores || 0],
      ['失败数', summary.failed || 0]
    ];
    document.getElementById('metrics').innerHTML = metrics.map(function (item) {
      return '<div class="metric"><span class="label">' + item[0] + '</span><span class="value">' + esc(item[1]) + '</span></div>';
    }).join('');
    document.getElementById('overall-progress').innerHTML =
      '<span class="bar">' + bar(summary.completion_rate) + '</span>';

    var chapters = summary.by_chapter || {};
    var names = Object.keys(chapters).sort();
    document.getElementById('chapter-progress').innerHTML = names.length ? names.map(function (name) {
      var item = chapters[name];
      var source = item.repeat_source === 'override' ? '覆盖' : '默认';
      return '<div>' + esc(name) + ' <span class="bar">' + bar(item.rate) + '</span> [' +
        item.done + '/' + item.total + ']（' + source + '）</div>';
    }).join('') : '暂无大章数据。';

    var masked = state.config_masked || {};
    var scope = masked.chapters || [];
    var limit = masked.limit || 0;
    var repeatDefault = masked.repeat_default || 3;
    var overrides = masked.repeat_overrides || {};
    var overrideText = Object.keys(overrides).map(function (name) {
      return name + ':' + overrides[name];
    }).join('、');
    document.getElementById('scope-line').textContent =
      '本次范围：' + (scope.length ? scope.join('、') : '全部大章') +
      '（限量 ' + limit + ' 章；默认每章 ' + repeatDefault + ' 次' +
      (overrideText ? '；覆盖 ' + overrideText : '') + '）';

    var rows = state.plan.map(function (task, index) {
      var record = recordFor(task);
      var score = (record.score === null || record.score === undefined) ? '—' : esc(record.score);
      var source = task.repeat_source === 'override' ? '（覆盖）' : '';
      return '<tr><td class="num">' + String(index + 1).padStart(2, '0') + '</td><td>' +
        esc(task.label || task.file_name) + source + '</td><td>' + esc(task.node_id) +
        '</td><td>' + statusHtml(record.status || 'pending') +
        '</td><td class="num">' + score + '</td></tr>';
    });
    document.getElementById('overview-rows').innerHTML = rows.length ? rows.join('') :
      '<tr><td colspan="5">暂无计划，请先执行 config / run。</td></tr>';

    var logs = state.logs || [];
    document.getElementById('log-tail').textContent = logs.length ? logs.join('\n') : '暂无日志。';
  }

  function renderRecords() {
    var keyword = (document.getElementById('record-search').value || '').trim().toLowerCase();
    var statusFilter = document.getElementById('record-status').value;
    var chapterFilter = document.getElementById('record-chapter').value;
    var chapters = {};
    state.plan.forEach(function (task) { chapters[task.chapter] = true; });
    var chapterSelect = document.getElementById('record-chapter');
    var selectedChapter = chapterSelect.value;
    chapterSelect.innerHTML = '<option value="">全部</option>' + Object.keys(chapters).sort().map(function (name) {
      return '<option value="' + esc(name) + '">' + esc(name) + '</option>';
    }).join('');
    chapterSelect.value = selectedChapter || chapterFilter;

    var repeatSelect = document.getElementById('record-repeat');
    var repeatFilter = repeatSelect.value;
    var repeats = {};
    state.plan.forEach(function (task) { repeats[task.repeat_index || 1] = true; });
    repeatSelect.innerHTML = '<option value="">全部</option>' + Object.keys(repeats).sort(function (a, b) {
      return Number(a) - Number(b);
    }).map(function (k) {
      return '<option value="' + esc(k) + '">第' + esc(k) + '次</option>';
    }).join('');
    repeatSelect.value = repeatFilter;

    var rows = [];
    state.plan.forEach(function (task, index) {
      var record = recordFor(task);
      var status = record.status || 'pending';
      var haystack = [task.file_name, task.chapter, record.error_summary, task.node_name,
        task.label, task.repeat_index, task.repeat_source].join(' ').toLowerCase();
      if (statusFilter && status !== statusFilter) { return; }
      if (chapterFilter && task.chapter !== chapterFilter) { return; }
      if (repeatFilter && String(task.repeat_index || 1) !== repeatFilter) { return; }
      if (keyword && haystack.indexOf(keyword) < 0) { return; }
      var score = (record.score === null || record.score === undefined) ? '—' : esc(record.score);
      var session = record.session_id ? '…' + esc(String(record.session_id).slice(-6)) : '—';
      var note = record.error_type ? esc(record.error_type + ' / ' + (record.error_summary || '')) : '';
      var source = task.repeat_source === 'override' ? '（覆盖）' : '';
      rows.push('<tr data-key="' + esc(task.unique_key) + '" data-note="' + esc(note) + '"><td><input type="checkbox" class="row-check"></td>' +
        '<td class="num">' + String(index + 1).padStart(2, '0') + '</td><td>' +
        esc(task.label || task.file_name) + source + '</td><td>' + esc(task.node_id) +
        '</td><td>' + statusHtml(status) + '</td><td class="num">' + score +
        '</td><td>' + session + '</td><td>' + note + '</td></tr>');
    });
    document.getElementById('record-rows').innerHTML = rows.length ? rows.join('') :
      '<tr><td colspan="8">没有匹配的复习记录。</td></tr>';
    document.getElementById('records-count').textContent = '共 ' + rows.length + ' 条记录';

    var table = document.getElementById('record-rows');
    table.onclick = function (event) {
      var tr = event.target.closest('tr[data-key]');
      if (!tr) { return; }
      document.getElementById('record-detail').innerHTML = '<strong>' + esc(tr.getAttribute('data-key')) +
        '</strong><br>' + (tr.getAttribute('data-note') || '暂无备注；失败原因只显示错误类型与可读摘要，不含凭据。') +
        '<br><button class="text-button" type="button" data-rerun-one="' + esc(tr.getAttribute('data-key')) + '">重新执行此任务</button>';
      var button = document.querySelector('[data-rerun-one]');
      if (button) {
        button.onclick = function () { rerun([button.getAttribute('data-rerun-one')]); };
      }
    };
  }

  function rerun(keys) {
    if (!keys.length || !window.confirm('确认将所选 ' + keys.length + ' 项登记为重新执行？')) { return; }
    fetch('/api/control', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'force_rerun', keys: keys })
    }).then(function (response) { return response.json(); }).then(function (data) {
      window.alert(data.message || (data.error ? '操作失败：' + data.error : '已提交'));
      poll();
    });
  }

  function loadBank() {
    var keyword = (document.getElementById('bank-search').value || '').trim();
    var qtype = document.getElementById('bank-qtype').value;
    var chapter = document.getElementById('bank-chapter').value;
    var select = document.getElementById('bank-chapter');
    var previous = select.value;
    var chapters = {};
    state.plan.forEach(function (task) { chapters[task.chapter] = true; });
    select.innerHTML = '<option value="">全部</option>' + Object.keys(chapters).sort().map(function (name) {
      return '<option value="' + esc(name) + '">' + esc(name) + '</option>';
    }).join('') + '<option value="__all">含其他章节</option>';
    select.value = previous || chapter;
    var query = '/api/questions?keyword=' + encodeURIComponent(keyword) +
      '&qtype=' + encodeURIComponent(qtype) +
      '&chapter=' + encodeURIComponent(chapter === '__all' ? '' : chapter);
    fetch(query).then(function (response) { return response.json(); }).then(function (data) {
      renderBank(data.questions || []);
    });
  }

  function renderBank(questions) {
    document.getElementById('bank-count').textContent =
      '共 ' + questions.length + ' 题（按大章分组，题干去重）';
    var list = document.getElementById('bank-list');
    if (!questions.length) {
      list.innerHTML = '<p class="muted">暂无题目；完成一次复习后，题目、正确答案与解析会自动保存在本机 data/bank.json。</p>';
      return;
    }
    // 按大章分组：同学复习时先看某一章，再展开题目看答案和解析。
    var groups = {};
    questions.forEach(function (q) {
      var chapter = q.chapter || '未分类';
      if (!groups[chapter]) { groups[chapter] = []; }
      groups[chapter].push(q);
    });
    var chapters = Object.keys(groups).sort(function (a, b) {
      return a.localeCompare(b, 'zh-CN');
    });
    var index = 0;
    list.innerHTML = chapters.map(function (chapter) {
      var items = groups[chapter].map(function (q) {
        index += 1;
        var answerText = String(q.correct_answer || '').trim();
        var answerParts = answerText.split(/[；;，,]/).map(function (part) {
          return part.trim();
        }).filter(Boolean);
        var lettersOnly = /^[A-Za-z](?:[,，、\s][A-Za-z])*$/.test(answerText);
        var answerLetters = answerText.toUpperCase().split(/[,，、\s]+/);
        var options = (q.options || []).map(function (option, position) {
          var letter = String.fromCharCode(65 + position);
          var isCorrect = lettersOnly
            ? answerLetters.indexOf(letter) >= 0
            : answerParts.some(function (part) {
              return part === option || (part.length >= 4 && option.indexOf(part) >= 0);
            });
          var image = /<img/i.test(option) ? ' <span class="muted">[含图片]</span>' : '';
          return '<li>' + letter + '. ' + esc(option) + image +
            (isCorrect ? ' <span class="mark">✓ 正确</span>' : '') + '</li>';
        }).join('');
        var typeText = q.qtype === 'qt02' ? '多选' : '单选';
        return '<details><summary>Q' + index + '（' + typeText + '） ' + esc(q.content).slice(0, 120) +
          '</summary><ol>' + options + '</ol>' +
          '<div class="answer"><p>正确答案：' + esc(q.correct_answer || '—') + '</p>' +
          '<p>解析：' + esc(q.analysis || '—') + '</p>' +
          '<p class="muted">来源：' + esc((q.sources || []).join(' / ')) + '　建议先自测，后看解析。</p></div></details>';
      }).join('');
      return '<section class="bank-group"><h2>' + esc(chapter) +
        '<span class="muted"> · ' + groups[chapter].length + ' 题</span></h2>' + items + '</section>';
    }).join('');
  }

  // ---------- 网页启动表单（v3 新增） ----------
  function buildStartGrid() {
    var chapterGrid = document.getElementById('chapter-grid');
    var overrideGrid = document.getElementById('override-grid');
    if (!chapterGrid || !overrideGrid) { return; }
    chapterGrid.innerHTML = CHAPTERS.map(function (item) {
      return '<label class="chapter-item"><input type="checkbox" class="chapter-check" value="' +
        esc(item.name) + '"> ' + esc(item.name) + '</label>';
    }).join('');
    overrideGrid.innerHTML = CHAPTERS.map(function (item) {
      return '<label class="chapter-item">' + esc(item.name) +
        '：<input class="override-input" data-chapter="' + esc(item.name) +
        '" type="number" min="1" max="99" step="1" placeholder="默认"></label>';
    }).join('');
  }

  function setAllChapters(checked) {
    var checks = document.querySelectorAll('.chapter-check');
    for (var i = 0; i < checks.length; i++) { checks[i].checked = !!checked; }
  }

  function prefillForm(masked) {
    if (formPrefilled || !masked) { return; }
    var username = document.getElementById('start-username');
    var repeat = document.getElementById('start-repeat');
    var limit = document.getElementById('start-limit');
    if (username && !username.value) { username.value = masked.username || ''; }
    if (repeat && !repeat.value) { repeat.value = masked.repeat_default || 3; }
    if (limit && !limit.value) { limit.value = (masked.limit === undefined ? 10 : masked.limit); }
    var selected = masked.chapters || [];
    var checks = document.querySelectorAll('.chapter-check');
    for (var i = 0; i < checks.length; i++) {
      var value = checks[i].value;
      var matched = selected.some(function (name) {
        var text = String(name);
        return text === value || text.indexOf(value) === 0 || value.indexOf(text) === 0;
      });
      if (matched) { checks[i].checked = true; }
    }
    // 密码永不预填；这里只碰非凭据字段。
    formPrefilled = true;
  }

  function renderRuntime(runtime) {
    if (!runtime) { return; }
    var phase = runtime.phase || 'not_started';
    var phaseLine = document.getElementById('runtime-phase-text');
    var snapshotBox = document.getElementById('runtime-snapshot');
    var errorBox = document.getElementById('runtime-error');
    var button = document.getElementById('btn-start');
    if (phaseLine) {
      phaseLine.textContent = '运行态：' + (runtime.phase_text || PHASE_TEXT[phase] || '未启动');
    }
    if (errorBox) {
      errorBox.textContent = runtime.error_type
        ? '失败原因：' + (runtime.error_summary || runtime.error_type) : '';
    }
    if (snapshotBox) {
      var snapshot = runtime.snapshot || {};
      var fields = [
        ['学号', snapshot.username], ['章节', snapshot.chapters],
        ['每章次数', snapshot.repeat_default], ['按章覆盖', snapshot.repeat_overrides],
        ['限量', snapshot.limit]
      ];
      var parts = [];
      fields.forEach(function (field) {
        var item = field[1];
        if (!item || item.value === undefined || item.value === null) { return; }
        var value = item.value;
        if (Array.isArray(value)) {
          value = value.length ? value.join('、') : '全部大章';
        } else if (typeof value === 'object') {
          value = Object.keys(value).map(function (key) {
            return key + ':' + value[key];
          }).join('、') || '无';
        }
        parts.push(field[0] + '：' + esc(value) + ' <span class="source-tag">' +
          esc(SOURCE_TEXT[item.source] || item.source || '') + '</span>');
      });
      var counts = snapshot.chapter_counts || [];
      if (counts.length) {
        parts.push('本次章节：' + counts.map(function (item) {
          return esc(item.chapter || item.node_id) + ' ' + esc(item.count) + ' 次（' +
            (item.source === 'override' ? '覆盖' : '默认') + '）';
        }).join('；'));
      }
      snapshotBox.innerHTML = parts.length ? '本次参数快照：' + parts.join('；') : '本次参数快照：尚未启动。';
    }
    var active = phase === 'starting' || phase === 'running' || startBusy;
    if (button) { button.disabled = active; }
  }

  function startRun(event) {
    if (event) { event.preventDefault(); }
    var button = document.getElementById('btn-start');
    var feedback = document.getElementById('start-feedback');
    var passwordInput = document.getElementById('start-password');
    if (startBusy || (button && button.disabled)) { return; }
    var username = String(document.getElementById('start-username').value || '').trim();
    var password = String(passwordInput.value || '');
    var repeatRaw = String(document.getElementById('start-repeat').value || '').trim();
    var limitRaw = String(document.getElementById('start-limit').value || '').trim();
    if (!username) { feedback.textContent = '请填写学号。'; return; }
    if (!password) { feedback.textContent = '请填写密码。'; return; }
    var repeatDefault = Number(repeatRaw);
    var limit = Number(limitRaw);
    if (!/^\d+$/.test(repeatRaw) || repeatDefault < 1 || repeatDefault > 99) {
      feedback.textContent = '每章次数须为 1~99 的整数。'; return;
    }
    if (!/^\d+$/.test(limitRaw) || limit < 0) {
      feedback.textContent = '限量须为非负整数（0 表示全量）。'; return;
    }
    var chapters = [];
    var checks = document.querySelectorAll('.chapter-check');
    for (var i = 0; i < checks.length; i++) {
      if (checks[i].checked) { chapters.push(checks[i].value); }
    }
    if (!chapters.length) { feedback.textContent = '请至少勾选一个章节（可点“全选”）。'; return; }
    var overrides = {};
    var inputs = document.querySelectorAll('.override-input');
    for (var j = 0; j < inputs.length; j++) {
      var raw = String(inputs[j].value || '').trim();
      if (!raw) { continue; }
      var count = Number(raw);
      if (!/^\d+$/.test(raw) || count < 1 || count > 99) {
        feedback.textContent = '按章覆盖次数须为 1~99 的整数。'; return;
      }
      overrides[inputs[j].getAttribute('data-chapter')] = count;
    }
    if (limit === 0 && !window.confirm('限量 0 表示本次纳入全部所选章节，可能耗时较长。确认继续？')) {
      return;
    }
    var payload = {
      username: username,
      password: password,
      chapters: chapters,
      repeat_default: repeatDefault,
      repeat_overrides: overrides,
      limit: limit,
      confirm_full: limit === 0
    };
    startBusy = true;
    button.disabled = true;
    feedback.textContent = '正在启动…';
    fetch('/api/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (response) { return response.json(); }).then(function (data) {
      passwordInput.value = '';  // 提交后立即清空密码框，绝不回显
      feedback.textContent = data && data.message ? data.message : '已提交。';
      poll(finishStart);
    }).catch(function () {
      passwordInput.value = '';
      feedback.textContent = '启动请求失败，请稍后重试。';
      poll(finishStart);
    });
  }

  function finishStart() {
    startBusy = false;
    renderRuntime(state.runtime);
  }

  function poll(onDone) {
    fetch('/api/state', { cache: 'no-store' }).then(function (response) {
      if (!response.ok) { throw new Error('HTTP ' + response.status); }
      return response.json();
    }).then(function (data) {
      state = data;
      document.body.classList.toggle('status-color', !!data.status_color);
      if (!formPrefilled) { prefillForm(data.config_masked); }
      renderRuntime(data.runtime);
      renderOverview();
      if (currentView === 'records') { renderRecords(); }
      if (currentView === 'bank') { loadBank(); }
    }).catch(function () {
      document.getElementById('log-tail').textContent = '状态读取失败，将在下一次轮询重试。';
    }).then(function () {
      if (typeof onDone === 'function') { onDone(); }
    });
  }

  function bind() {
    var tabs = document.querySelectorAll('.tab');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener('click', function () { switchView(this.getAttribute('data-view')); });
    }
    var goto = document.querySelector('[data-goto]');
    if (goto) { goto.addEventListener('click', function () { switchView(goto.getAttribute('data-goto')); }); }
    ['record-search', 'record-status', 'record-chapter', 'record-repeat'].forEach(function (id) {
      var element = document.getElementById(id);
      if (element) { element.addEventListener('input', renderRecords); element.addEventListener('change', renderRecords); }
    });
    ['bank-search', 'bank-qtype', 'bank-chapter'].forEach(function (id) {
      var element = document.getElementById(id);
      if (element) { element.addEventListener('input', loadBank); element.addEventListener('change', loadBank); }
    });
    document.getElementById('btn-export-csv').addEventListener('click', function () { window.location = '/api/export?fmt=csv'; });
    document.getElementById('btn-export-html').addEventListener('click', function () { window.location = '/api/export?fmt=html'; });
    document.getElementById('btn-export-md').addEventListener('click', function () { window.location = '/api/export?fmt=markdown'; });
    document.getElementById('btn-export-json').addEventListener('click', function () { window.location = '/api/export?fmt=json'; });
    document.getElementById('btn-rerun').addEventListener('click', function () {
      var checks = document.querySelectorAll('.row-check:checked');
      var keys = [];
      var rows = document.querySelectorAll('#record-rows tr[data-key]');
      for (var i = 0; i < checks.length; i++) {
        var row = checks[i].closest('tr');
        if (row) { keys.push(row.getAttribute('data-key')); }
      }
      rerun(keys);
    });
    var startForm = document.getElementById('start-form');
    if (startForm) { startForm.addEventListener('submit', startRun); }
    var allButton = document.getElementById('btn-chapter-all');
    if (allButton) { allButton.addEventListener('click', function () { setAllChapters(true); }); }
    var noneButton = document.getElementById('btn-chapter-none');
    if (noneButton) { noneButton.addEventListener('click', function () { setAllChapters(false); }); }
  }

  document.addEventListener('DOMContentLoaded', function () {
    buildStartGrid();
    bind();
    poll();
    var interval = Math.min(10000, Math.max(1000, (state.poll_sec || 3) * 1000));
    window.setInterval(poll, interval);
  });
}());
