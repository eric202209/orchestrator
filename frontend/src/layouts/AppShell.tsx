import { useState } from 'react';
import { Outlet, Link, useLocation } from 'react-router-dom';
import { 
  LayoutDashboard, 
  GitBranch, 
  Terminal, 
  ListTodo, 
  Menu, 
  X,
  Activity,
  Settings
} from 'lucide-react';
import { cn } from '@/lib/utils';

const navItems = [
  {
    title: 'Dashboard',
    href: '/',
    icon: LayoutDashboard,
  },
  {
    title: 'Projects',
    href: '/projects',
    icon: GitBranch,
  },
  {
    title: 'Tasks',
    href: '/tasks',
    icon: ListTodo,
  },
  {
    title: 'Sessions',
    href: '/sessions',
    icon: Terminal,
  },
  {
    title: 'Settings',
    href: '/settings',
    icon: Settings,
  },
];

export default function AppShell() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const location = useLocation();

  return (
    <div className="min-h-screen bg-slate-950 flex">
      {/* Desktop Sidebar */}
      <aside className="hidden md:flex md:w-56 md:flex-col md:fixed md:inset-y-0 bg-slate-950 border-r border-slate-800/60">
        <div className="flex flex-col flex-1 min-h-0">
          {/* Logo */}
          <div className="flex items-center gap-2 h-14 px-5 border-b border-slate-800/60">
            <Activity className="h-5 w-5 text-sky-500" />
            <span className="text-sm font-semibold text-white tracking-tight">Orchestrator</span>
          </div>

          {/* Navigation */}
          <nav className="flex-1 px-3 py-4 space-y-0.5">
            {navItems.map((item) => {
              const isActive = location.pathname === item.href ||
                              (item.href !== '/' && location.pathname.startsWith(item.href));

              return (
                <Link
                  key={item.href}
                  to={item.href}
                  className={cn(
                    'flex items-center gap-2.5 px-3 py-2 rounded-md transition-colors text-sm',
                    isActive
                      ? 'bg-slate-800 text-white font-medium'
                      : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-200'
                  )}
                >
                  <item.icon className={cn('h-4 w-4 flex-shrink-0', isActive ? 'text-sky-400' : '')} />
                  <span>{item.title}</span>
                </Link>
              );
            })}
          </nav>

          {/* Footer */}
          <div className="px-5 py-3 border-t border-slate-800/60">
            <div className="text-xs text-slate-600">v1.0.0</div>
          </div>
        </div>
      </aside>

      {/* Mobile Drawer */}
      {mobileMenuOpen && (
        <>
          <div
            className="fixed inset-0 bg-black/60 z-40 md:hidden"
            onClick={() => setMobileMenuOpen(false)}
          />
          <div className="fixed inset-y-0 left-0 w-56 bg-slate-950 border-r border-slate-800/60 z-50 md:hidden">
            <div className="flex items-center justify-between h-14 px-4 border-b border-slate-800/60">
              <div className="flex items-center gap-2">
                <Activity className="h-5 w-5 text-sky-500" />
                <span className="text-sm font-semibold text-white">Orchestrator</span>
              </div>
              <button
                onClick={() => setMobileMenuOpen(false)}
                className="text-slate-400 hover:text-white"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            <nav className="p-3 space-y-0.5">
              {navItems.map((item) => {
                const isActive = location.pathname === item.href ||
                                (item.href !== '/' && location.pathname.startsWith(item.href));

                return (
                  <Link
                    key={item.href}
                    to={item.href}
                    onClick={() => setMobileMenuOpen(false)}
                    className={cn(
                      'flex items-center gap-2.5 px-3 py-2 rounded-md transition-colors text-sm',
                      isActive
                        ? 'bg-slate-800 text-white font-medium'
                        : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-200'
                    )}
                  >
                    <item.icon className={cn('h-4 w-4 flex-shrink-0', isActive ? 'text-sky-400' : '')} />
                    <span>{item.title}</span>
                  </Link>
                );
              })}
            </nav>
          </div>
        </>
      )}

      {/* Main Content */}
      <div className="flex-1 md:ml-56">
        {/* Mobile Header */}
        <header className="md:hidden h-14 bg-slate-950 border-b border-slate-800/60 sticky top-0 z-30">
          <div className="flex items-center justify-between h-full px-4">
            <button
              onClick={() => setMobileMenuOpen(true)}
              className="text-slate-400 hover:text-white"
            >
              <Menu className="h-5 w-5" />
            </button>
            <div className="flex items-center gap-2">
              <Activity className="h-5 w-5 text-sky-500" />
              <span className="text-sm font-semibold text-white">Orchestrator</span>
            </div>
            <div className="w-5" />
          </div>
        </header>

        {/* Page Content */}
        <main className="p-5 sm:p-6 lg:p-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
