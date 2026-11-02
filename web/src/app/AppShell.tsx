import { clsx } from "clsx";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { canIn, Perm, useMe, type Me } from "@/features/session";
import { Banner, Button, Icon, Menu, Spinner } from "@/ui";
import { toNavItems, type NavItem } from "./nav";
import { setTheme } from "./theme";
import s from "./AppShell.module.css";

function Brand() {
  return (
    <Link to="/" className={s.brand} aria-label="LessonForge home">
      <span className={s.mark} aria-hidden />
      <span className={s.wordmark}>LessonForge</span>
    </Link>
  );
}

function initials(name: string) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]!.toUpperCase()).join("") || "?";
}

function signOut(url: string) {
  // The logout route is a POST that clears the cookie and redirects to Edboox.
  const form = document.createElement("form");
  form.method = "POST";
  form.action = url;
  document.body.append(form);
  form.submit();
}

function UserMenu({ me, compact }: { me: Me; compact?: boolean }) {
  const navigate = useNavigate();
  const name = me.name || me.subject;
  const trigger = compact
    ? <button type="button" className={s.user} aria-label="Account" style={{ width: "auto", padding: 4 }}><span className={s.avatar}>{initials(name)}</span></button>
    : (
      <button type="button" className={s.user}>
        <span className={s.avatar}>{initials(name)}</span>
        <span className={s.userText}><span className={s.userName}>{name}</span><span className={s.userRole}>{me.role}</span></span>
        <Icon.ChevronUp size={16} style={{ marginLeft: "auto", color: "var(--ink-3)" }} />
      </button>
    );
  return (
    <Menu align={compact ? "end" : "start"} trigger={trigger} items={[
      { heading: `${name} · ${me.role}` },
      ...(canIn(me, Perm.author) ? [{ label: "Teaching profile", icon: <Icon.User size={16} />, onSelect: () => navigate("/settings") }] : []),
      { label: "Light theme", onSelect: () => setTheme("light") },
      { label: "Dark theme", onSelect: () => setTheme("dark") },
      { label: "Match device", onSelect: () => setTheme("system") },
      "separator",
      { label: "Sign out", icon: <Icon.LogOut size={16} />, onSelect: () => signOut(me.logout_url) },
    ]} />
  );
}

function Rail({ items, me, pathname }: { items: NavItem[]; me: Me; pathname: string }) {
  return (
    <aside className={s.rail} aria-label="Main">
      <Brand />
      <nav>
        <ul className={s.navList}>
          {items.map((it) => (
            <li key={it.path}>
              <NavLink to={it.path} className={s.navLink} aria-current={it.match(pathname) ? "page" : undefined}>
                {it.icon}{it.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <div className={s.railFoot}><UserMenu me={me} /></div>
    </aside>
  );
}

function TabBar({ items, pathname }: { items: NavItem[]; pathname: string }) {
  const navigate = useNavigate();
  const MAX = 5;
  const shown = items.length > MAX ? items.slice(0, MAX - 1) : items;
  const rest = items.length > MAX ? items.slice(MAX - 1) : [];
  return (
    <nav className={s.tabbar} aria-label="Main">
      {shown.map((it) => (
        <NavLink key={it.path} to={it.path} className={s.tab} aria-current={it.match(pathname) ? "page" : undefined}>
          {it.icon}<span>{it.label}</span>
        </NavLink>
      ))}
      {rest.length > 0 && (
        <Menu trigger={<button type="button" className={s.tab} style={{ border: 0, background: "none" }}><Icon.MoreHorizontal size={18} /><span>More</span></button>}
          items={rest.map((it) => ({ label: it.label, icon: it.icon, onSelect: () => navigate(it.path) }))} />
      )}
    </nav>
  );
}

/** The standalone app's frame: rail on desktop, top bar + tab bar on phones. */
export function AppShell() {
  const { data: me, error, isPending, refetch } = useMe();
  const { pathname } = useLocation();

  if (isPending) return <div className={s.boot}><Spinner label="Signing you in" /></div>;
  if (error || !me) {
    // A 401 is already redirecting to Edboox; anything else gets a retry.
    return (
      <div className={s.boot}>
        <Banner tone="danger" title="LessonForge couldn't start" actions={<Button size="sm" onClick={() => refetch()}>Try again</Button>}>
          We couldn't confirm who you are. If this keeps happening, open LessonForge again from Edboox.
        </Banner>
      </div>
    );
  }

  const items = toNavItems(me.nav);
  const hasNav = items.length > 1;
  return (
    <div className={clsx(s.shell, hasNav && s.hasNav)}>
      <a href="#main" className={s.skip}>Skip to content</a>
      {hasNav && <Rail items={items} me={me} pathname={pathname} />}
      <div className={s.main}>
        <header className={s.topbar}>
          <Brand />
          <UserMenu me={me} compact />
        </header>
        <main id="main" tabIndex={-1} style={{ outline: "none" }}><Outlet /></main>
      </div>
      {hasNav && <TabBar items={items} pathname={pathname} />}
    </div>
  );
}
