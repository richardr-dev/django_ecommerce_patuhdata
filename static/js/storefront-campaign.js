// Countdowns: any [data-campaign][data-ends-at] element fills its [data-days], [data-hours],
// [data-total-hours], [data-minutes] and [data-seconds] children every second, and hides
// itself (and its popup) when the campaign ends.
const UNITS = {
  days: [86400000, Infinity],
  hours: [3600000, 24],
  totalHours: [3600000, Infinity],
  minutes: [60000, 60],
  seconds: [1000, 60],
};
document.querySelectorAll('[data-campaign]').forEach(campaign => {
  const end = new Date(campaign.dataset.endsAt).getTime();
  const update = () => {
    const remaining = Math.max(0, end - Date.now());
    Object.entries(UNITS).forEach(([name, [size, modulo]]) => {
      const attribute = name.replace(/[A-Z]/g, letter => `-${letter.toLowerCase()}`);
      const node = campaign.querySelector(`[data-${attribute}]`);
      if (node) node.textContent = String(Math.floor(remaining / size) % modulo).padStart(2, '0');
    });
    if (!remaining) {
      campaign.hidden = true;
      document.querySelector(`[data-popup-for="${campaign.dataset.campaign}"]`)?.setAttribute('hidden', '');
    }
  };
  update();
  setInterval(update, 1000);
});
document.querySelectorAll('[data-campaign-popup]').forEach(popup=>{const key=`campaign_dismissed_${popup.dataset.campaignPopup}`;if(!localStorage.getItem(key))setTimeout(()=>popup.hidden=false,700);const dismiss=()=>{popup.hidden=true;localStorage.setItem(key,String(Date.now()))};popup.querySelectorAll('[data-popup-close]').forEach(button=>button.addEventListener('click',dismiss));popup.addEventListener('click',event=>{if(event.target===popup)dismiss()});document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!popup.hidden)dismiss()});popup.querySelector('[data-copy-code]')?.addEventListener('click',async event=>{await navigator.clipboard.writeText(event.currentTarget.dataset.copyCode);event.currentTarget.textContent='Tersalin'})});
