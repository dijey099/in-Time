// Dates are typed and shown as dd/mm/yyyy; the calendar button opens the browser's date picker
// (whose own display follows the browser language) and copies the chosen day back as dd/mm/yyyy.
document.querySelectorAll('.date-field').forEach((field) => {
  const text = field.querySelector('input[name]'), picker = field.querySelector('input[type=date]');
  const toIso = (v) => { const m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(v.trim());
                         return m ? `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}` : ''; };
  field.querySelector('.date-pick').onclick = () => {
    picker.value = toIso(text.value) || picker.value;
    try { picker.showPicker(); } catch (e) { picker.focus(); picker.click(); }
  };
  picker.onchange = () => {
    const [y, m, d] = picker.value.split('-');
    if (y) text.value = `${d}/${m}/${y}`;
  };
});
