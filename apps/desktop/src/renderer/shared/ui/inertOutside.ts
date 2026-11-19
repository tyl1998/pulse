const leases = new WeakMap<Element, { count: number; original: string | null }>();

/** React 19 callback-ref cleanup complements Radix's aria-hidden with native focus isolation. */
export function inertOutside(node: HTMLElement | null): (() => void) | undefined {
  if (!node) return undefined;
  const outside: Element[] = [];
  let current: Element | null = node;
  while (current && current !== node.ownerDocument.body) {
    const parent: HTMLElement | null = current.parentElement;
    if (!parent) break;
    for (const sibling of parent.children) {
      if (sibling === current) continue;
      const lease = leases.get(sibling) ?? { count: 0, original: sibling.getAttribute('inert') };
      lease.count += 1;
      leases.set(sibling, lease);
      sibling.setAttribute('inert', '');
      outside.push(sibling);
    }
    current = parent;
  }
  return () => {
    for (const element of outside) {
      const lease = leases.get(element);
      if (!lease || --lease.count > 0) continue;
      if (lease.original === null) element.removeAttribute('inert');
      else element.setAttribute('inert', lease.original);
      leases.delete(element);
    }
  };
}
