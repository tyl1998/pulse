import { Link } from '@tanstack/react-router';
import { NAV_ITEMS } from './navItems';

export function SideNav() {
  return (
    <nav className="app-nav" aria-label="主导航">
      <ul>
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          return (
            <li key={item.to}>
              <Link
                to={item.to}
                className="app-nav-link"
                activeProps={{ className: 'app-nav-link is-active', 'aria-current': 'page' }}
                activeOptions={{ exact: item.to === '/' }}
              >
                <Icon aria-hidden="true" />
                <span className="app-nav-label">{item.label}</span>
              </Link>
            </li>
          );
        })}
      </ul>
      <p className="app-nav-footnote">应用壳 FE-P2.3 · 布局保存在本机</p>
    </nav>
  );
}
