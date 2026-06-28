import { useState } from 'react';
import { Outlet, Link, useLocation } from 'react-router-dom';
import {
  LayoutDashboard, GitBranch, Terminal,
  ListTodo, Menu, X, Activity, Settings, FlaskConical, BarChart2
} from 'lucide-react';
import { cn } from '@/lib/utils';

const navItems = [
  { title: 'Dashboard', href: '/dashboard', icon: LayoutDashboard },
  { title: 'Projects',  href: '/projects',  icon: GitBranch },
  { title: 'Tasks',     href: '/tasks',     icon: ListTodo },
  { title: 'Sessions',  href: '/sessions',  icon: Terminal },
  { title: 'Analytics', href: '/analytics', icon: BarChart2 },
  { title: 'Pilot',    href: '/admin/pilot-dashboard', icon: FlaskConical },
  { title: 'Settings',  href: '/settings',  icon: Settings },
];

function NavLink({ item, isActive, onClick }: {
  item: typeof navItems[0];
  isActive: boolean;
  onClick?: () => void;
}) {
  return (
    <Link
      to={item.href}
      onClick={onClick}
      className={cn(
        'group flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors',
        isActive
          ? 'bg-[color:var(--oc-surface-raised)] text-white font-medium shadow-[inset_3px_0_0_var(--oc-accent)]'
          : 'text-slate-500 hover:bg-[color:var(--oc-surface-raised)] hover:text-slate-300'
      )}
    >
      <item.icon className={cn(
        'h-4 w-4 flex-shrink-0 transition-colors',
        isActive ? 'text-[color:var(--oc-accent)]' : 'text-slate-600 group-hover:text-slate-400'
      )} />
      <span>{item.title}</span>
    </Link>
  );
}

export default function AppShell() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const location = useLocation();
  const isActive = (href: string) =>
    location.pathname === href || (href !== '/' && location.pathname.startsWith(href));

  return (
    <div className="min-h-screen bg-[color:var(--oc-canvas)] flex">
      {/* Desktop Sidebar */}
      <aside className="hidden md:flex md:w-56 md:flex-col md:fixed md:inset-y-0 bg-[color:var(--oc-shell)] border-r border-[color:var(--oc-border-soft)]">
        <div className="flex flex-col flex-1 min-h-0">
          <div className="flex items-center gap-2.5 h-14 px-5 border-b border-[color:var(--oc-border-soft)]">
            <Activity className="h-4 w-4 text-[color:var(--oc-accent)] flex-shrink-0" />
            <span className="text-sm font-semibold text-white">Orchestrator</span>
          </div>

          <nav className="flex-1 px-2.5 py-4 space-y-0.5">
            {navItems.map((item) => (
              <NavLink key={item.href} item={item} isActive={isActive(item.href)} />
            ))}
          </nav>

          <div className="px-5 py-3 border-t border-[color:var(--oc-border-soft)]">
            <span className="text-xs font-mono text-slate-600">v1.0.0</span>
          </div>
        </div>
      </aside>

      {/* Mobile Drawer */}
      {mobileMenuOpen && (
        <>
          <div
            className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 md:hidden"
            onClick={() => setMobileMenuOpen(false)}
          />
          <div className="fixed inset-y-0 left-0 w-56 bg-[color:var(--oc-shell)] border-r border-[color:var(--oc-border-soft)] z-50 flex flex-col md:hidden">
            <div className="flex items-center justify-between h-14 px-4 border-b border-[color:var(--oc-border-soft)]">
              <div className="flex items-center gap-2.5">
                <Activity className="h-4 w-4 text-[color:var(--oc-accent)]" />
                <span className="text-sm font-semibold text-white">Orchestrator</span>
              </div>
              <button onClick={() => setMobileMenuOpen(false)} className="text-slate-500 hover:text-white transition-colors">
                <X className="h-5 w-5" />
              </button>
            </div>
            <nav className="flex-1 px-2.5 py-4 space-y-0.5">
              {navItems.map((item) => (
                <NavLink key={item.href} item={item} isActive={isActive(item.href)}
                  onClick={() => setMobileMenuOpen(false)} />
              ))}
            </nav>
          </div>
        </>
      )}

      {/* Main Content */}
      <div className="min-w-0 flex-1 overflow-x-hidden md:ml-56">
        <header className="md:hidden h-14 bg-[color:var(--oc-shell)] border-b border-[color:var(--oc-border-soft)] sticky top-0 z-30">
          <div className="flex items-center justify-between h-full px-4">
            <button onClick={() => setMobileMenuOpen(true)} className="text-slate-500 hover:text-white transition-colors">
              <Menu className="h-5 w-5" />
            </button>
            <div className="flex items-center gap-2">
              <Activity className="h-4 w-4 text-[color:var(--oc-accent)]" />
              <span className="text-sm font-semibold text-white">Orchestrator</span>
            </div>
            <div className="w-5" />
          </div>
        </header>

        <main className="box-border w-full max-w-full p-5 sm:p-6 lg:p-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
