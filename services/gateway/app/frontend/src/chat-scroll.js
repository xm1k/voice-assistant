export function captureScrollAnchor(container) {
  const top = container.getBoundingClientRect().top;
  const element = [...container.querySelectorAll('[data-message-id]')].find(item => item.getBoundingClientRect().bottom > top);
  return {id: element?.dataset.messageId, offset: element ? element.getBoundingClientRect().top - top : 0, height: container.scrollHeight, scrollTop: container.scrollTop};
}

export function restoreScrollAnchor(container, anchor) {
  const element = [...container.querySelectorAll('[data-message-id]')].find(item => item.dataset.messageId === anchor.id);
  if (element) container.scrollTop += element.getBoundingClientRect().top - container.getBoundingClientRect().top - anchor.offset;
  else container.scrollTop = anchor.scrollTop + container.scrollHeight - anchor.height;
}

export function nearBottom(container) {
  return container.scrollHeight - container.clientHeight - container.scrollTop < 96;
}
