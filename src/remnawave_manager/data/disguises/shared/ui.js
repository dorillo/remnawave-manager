let timer;
export function announce(message, timeout = 2800) {
  let region = document.querySelector('[data-shared-toast]');
  if (!region) {
    region = document.createElement('div');
    region.dataset.sharedToast = '';
    region.className = 'shared-toast';
    region.setAttribute('role', 'status');
    document.body.append(region);
  }
  clearTimeout(timer);
  region.textContent = message;
  region.hidden = false;
  timer = setTimeout(() => { region.hidden = true; }, timeout);
}
export function debounce(callback, delay = 200) {
  let handle;
  return (...args) => { clearTimeout(handle); handle = setTimeout(() => callback(...args), delay); };
}
export function bindDismissible(container, closeSelector = '[data-close],.close,.modal-close') {
  container.querySelectorAll(closeSelector).forEach((button) => button.addEventListener('click', () => { container.hidden = true; }));
  container.addEventListener('click', (event) => { if (event.target === container) container.hidden = true; });
}
