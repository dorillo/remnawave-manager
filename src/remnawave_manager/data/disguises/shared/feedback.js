// The label remains available to assistive technology and reduced-motion users.
export function loadingNode(label, className = '') {
  const node = document.createElement('span');
  node.className = `ui-loading ${className}`.trim();
  node.setAttribute('role', 'status');
  const spinner = document.createElement('span');
  spinner.className = 'ui-spinner';
  spinner.setAttribute('aria-hidden', 'true');
  node.append(spinner, document.createTextNode(label));
  return node;
}

export function loadingMarkup(label) {
  return loadingNode(label).outerHTML;
}
