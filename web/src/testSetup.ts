import { vi } from 'vitest';
Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
  value() {
    this.open = true;
  },
});
Object.defineProperty(HTMLDialogElement.prototype, 'close', {
  value() {
    this.open = false;
  },
});
Object.defineProperty(HTMLElement.prototype, 'showPopover', { value() {} });
Object.defineProperty(HTMLElement.prototype, 'hidePopover', { value() {} });
Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { value: vi.fn() });
