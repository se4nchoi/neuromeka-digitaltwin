(() => {
  const byId = id => document.getElementById(id);
  let online = false;
  let lastPacketAt = 0;
  function result(data) {
    byId('commandResult').textContent = `${data.cmd || 'Command'}: ${data.status}${data.message ? ' — ' + data.message : ''}`;
  }
  async function send(cmd, payload = {}) {
    try {
      const response = await fetch('/api/commands', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cmd, ...payload })
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      result(await response.json());
    } catch (error) {
      byId('commandResult').textContent = `Command unavailable: ${error.message}`;
    }
  }
  window.addEventListener('workcell-command', event => result(event.detail));
  window.addEventListener('workcell-telemetry', event => {
    const data = event.detail;
    online = true;
    lastPacketAt = Date.now();
    byId('workcellState').textContent = data.workcell_state;
    const fault = data.fault;
    byId('workcellFault').textContent = fault
      ? `${fault.code}: ${fault.message}. Interrupted step: ${fault.step}. ${fault.recovery}`
      : 'No active fault';
    if (fault) byId('workcellDiagnostics').open = true;
    byId('injectFault').disabled = data.mode !== 'SIMULATION' || Boolean(fault);
    byId('resetDemoCell').disabled = data.mode !== 'SIMULATION' || data.workcell_state === 'RUNNING';
    if (data.last_command) result(data.last_command);
  });
  byId('injectFault').onclick = () => send('inject_fault', { code: byId('faultCode').value });
  byId('resetDemoCell').onclick = () => send('reset_pallet');
  byId('recoverCell').onclick = () => send('recover');
  setInterval(() => {
    if (Date.now() - lastPacketAt > 3000) {
      online = false;
      byId('workcellState').textContent = 'OFFLINE';
    }
    if (!online) {
      byId('injectFault').disabled = true;
      byId('resetDemoCell').disabled = true;
    }
  }, 1000);
})();
