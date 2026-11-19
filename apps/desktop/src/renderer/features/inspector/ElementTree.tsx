import { useDeferredValue, useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { ChevronDown, ChevronRight, Search } from 'lucide-react';
import { Button, Input } from '../../shared/ui';
import { visibleInspectionElements, type InspectionSnapshot } from './inspectionModel';
import type { ElementInspectorStore } from './inspectorStore';

const ROW_HEIGHT = 32;
const OVERSCAN = 4;

export function ElementTree({
  snapshot,
  selectedId,
  store,
}: {
  snapshot: InspectionSnapshot;
  selectedId: string | null;
  store: ElementInspectorStore;
}) {
  const id = useId();
  const treeRef = useRef<HTMLDivElement>(null);
  const [search, setSearch] = useState('');
  const query = useDeferredValue(search);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const [focusedId, setFocusedId] = useState<string | null>(selectedId);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(192);
  const rows = visibleInspectionElements(snapshot, query, collapsed);
  const focusIndex = Math.max(
    0,
    rows.findIndex((node) => node.id === focusedId),
  );
  const active = rows[focusIndex];
  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const end = Math.min(rows.length, Math.ceil((scrollTop + height) / ROW_HEIGHT) + OVERSCAN);
  const rendered = rows.slice(start, end);
  if (active && (focusIndex < start || focusIndex >= end)) rendered.push(active);

  useEffect(() => {
    const tree = treeRef.current;
    if (!tree) return;
    const resize = () => {
      if (tree.clientHeight > 0) setHeight(tree.clientHeight);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(tree);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!selectedId) return;
    const frame = requestAnimationFrame(() => {
      setFocusedId(selectedId);
      setCollapsed((previous) => {
        const next = new Set(previous);
        let parent = snapshot.byId.get(selectedId)?.parentId;
        while (parent) {
          next.delete(parent);
          parent = snapshot.byId.get(parent)?.parentId;
        }
        return next;
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [snapshot, selectedId]);
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const tree = treeRef.current;
      if (!tree) return;
      const top = focusIndex * ROW_HEIGHT;
      const next =
        top < tree.scrollTop
          ? top
          : top + ROW_HEIGHT > tree.scrollTop + height
            ? top + ROW_HEIGHT - height
            : tree.scrollTop;
      tree.scrollTop = Math.max(0, Math.min(next, rows.length * ROW_HEIGHT - height));
      setScrollTop(tree.scrollTop);
    });
    return () => cancelAnimationFrame(frame);
  }, [focusIndex, height, rows.length]);

  const toggle = (nodeId: string) =>
    setCollapsed((previous) => {
      const next = new Set(previous);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });
  const keyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (
      event.target !== event.currentTarget ||
      !active ||
      event.nativeEvent.isComposing ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey
    )
      return;
    let next = focusIndex;
    switch (event.key) {
      case 'ArrowDown':
        next = Math.min(rows.length - 1, focusIndex + 1);
        break;
      case 'ArrowUp':
        next = Math.max(0, focusIndex - 1);
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = rows.length - 1;
        break;
      case 'ArrowRight':
        if (active.hasChildren && collapsed.has(active.id) && !query.trim()) toggle(active.id);
        else if (rows[focusIndex + 1]?.parentId === active.id) next = focusIndex + 1;
        break;
      case 'ArrowLeft':
        if (active.hasChildren && !collapsed.has(active.id) && !query.trim()) toggle(active.id);
        else if (active.parentId)
          next = Math.max(
            0,
            rows.findIndex((node) => node.id === active.parentId),
          );
        break;
      case 'Enter':
      case ' ':
        store.select(active.id);
        break;
      default:
        return;
    }
    event.preventDefault();
    setFocusedId(rows[next]?.id ?? null);
  };

  return (
    <section className="element-tree" aria-label="元素树浏览器">
      <div className="element-tree-search">
        <Search aria-hidden="true" />
        <Input
          size="sm"
          type="search"
          aria-label="搜索元素"
          placeholder="文本、ID、类名或路径"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setFocusedId(null);
          }}
        />
      </div>
      <div className="element-tree-tools">
        <span>
          {rows.length} / {snapshot.elements.length} 节点
        </span>
        <Button size="sm" variant="ghost" onClick={() => setCollapsed(new Set())}>
          展开全部元素
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() =>
            setCollapsed(
              new Set(snapshot.elements.filter((node) => node.hasChildren).map((node) => node.id)),
            )
          }
        >
          折叠全部元素
        </Button>
      </div>
      <div
        ref={treeRef}
        className="element-tree-viewport"
        role="tree"
        aria-label="元素树"
        tabIndex={0}
        aria-activedescendant={active ? `${id}-${active.id}` : undefined}
        onKeyDown={keyboard}
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
      >
        <div
          className="element-tree-spacer"
          role="presentation"
          style={{ height: rows.length * ROW_HEIGHT }}
        >
          {rendered.map((node) => {
            const index = rows.indexOf(node);
            const expanded = query.trim() !== '' || !collapsed.has(node.id);
            return (
              <div
                key={node.id}
                id={`${id}-${node.id}`}
                role="treeitem"
                aria-level={node.depth + 1}
                aria-label={`${node.label} ${node.source === 'native' ? '原生' : 'OCR'}`}
                aria-selected={node.id === selectedId}
                aria-expanded={node.hasChildren ? expanded : undefined}
                className="element-tree-row"
                data-focused={node.id === active?.id}
                data-source={node.source}
                style={{
                  top: index * ROW_HEIGHT,
                  height: ROW_HEIGHT,
                  paddingLeft: 4 + Math.min(node.depth, 8) * 12,
                }}
                onClick={() => {
                  setFocusedId(node.id);
                  store.select(node.id);
                  treeRef.current?.focus();
                }}
                onPointerEnter={() => store.hover(node.id)}
                onPointerLeave={() => store.hover(null)}
              >
                {node.hasChildren ? (
                  <button
                    type="button"
                    className="element-tree-disclosure"
                    tabIndex={-1}
                    aria-label={`${expanded ? '折叠' : '展开'} ${node.label}`}
                    disabled={!!query.trim()}
                    onClick={(event) => {
                      event.stopPropagation();
                      setFocusedId(node.id);
                      toggle(node.id);
                      treeRef.current?.focus();
                    }}
                  >
                    {expanded ? (
                      <ChevronDown aria-hidden="true" />
                    ) : (
                      <ChevronRight aria-hidden="true" />
                    )}
                  </button>
                ) : (
                  <span className="element-tree-disclosure" aria-hidden="true" />
                )}
                <span className="element-tree-label" title={node.resId ?? node.label}>
                  {node.label}
                </span>
                <span className="element-tree-source">
                  {node.source === 'native' ? '原生' : 'OCR'}
                </span>
              </div>
            );
          })}
        </div>
      </div>
      {rows.length === 0 ? (
        <p className="inspector-note" role="status">
          没有匹配的已采集节点；这不证明页面上不存在该元素。
        </p>
      ) : null}
      <p className="inspector-note">
        方向键浏览和展开，Enter 或空格选中；搜索会保留匹配节点的父路径。
      </p>
    </section>
  );
}
