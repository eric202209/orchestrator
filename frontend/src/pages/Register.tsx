import { useState } from 'react';
import { Link } from 'react-router-dom';
import { authAPI } from '../api/client';
import { User, ArrowRight, Info, Eye, EyeOff } from 'lucide-react';

function Register() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState(false);
  const [loading, setLoading] = useState(false);
  const [showRequirements, setShowRequirements] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    if (password !== confirmPassword) {
      setError('Passwords do not match');
      return;
    }

    if (password.length < 6) {
      setError('Password must be at least 6 characters');
      return;
    }

    setLoading(true);

    try {
      await authAPI.register(email, password);
      setSuccess(true);
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string | Array<{ msg?: string }> | object } }; message?: string };
      console.error('Registration error:', error);
      // Handle different error response formats
      const detail = error.response?.data?.detail;
      if (Array.isArray(detail)) {
        const msg = detail[0]?.msg || detail[0];
        // Check if it's an "already registered" error
        if (msg && msg.includes('already registered')) {
          setError(msg);
        } else {
          setError(msg || 'Registration failed. Please check your email format and try again.');
        }
      } else if (typeof detail === 'object' && detail !== null) {
        setError(JSON.stringify(detail) || 'Registration failed. Please check your email format and try again.');
      } else if (detail) {
        setError(String(detail));
      } else {
        setError(error.message || 'Registration failed. Please check your email format and try again.');
      }
    } finally {
      setLoading(false);
    }
  };

  if (success) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-primary-900 to-slate-900 p-4">
        <div className="max-w-md w-full bg-slate-800/50 backdrop-blur rounded-xl p-8 shadow-xl border border-slate-700 text-center">
          <div className="mx-auto h-16 w-16 bg-green-500 rounded-full flex items-center justify-center mb-4">
            <User className="h-8 w-8 text-white" />
          </div>
          <h2 className="text-2xl font-bold text-white mb-2">Account Created!</h2>
          <p className="text-slate-400 mb-6">
            Your account has been created successfully. You can now sign in.
          </p>
          <Link
            to="/login"
            className="inline-flex items-center gap-2 bg-primary-500 hover:bg-primary-600 text-white font-semibold py-3 px-6 rounded-lg transition-all"
          >
            Sign in <ArrowRight className="h-4 w-4" />
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-primary-900 to-slate-900 p-4">
      <div className="max-w-md w-full space-y-8">
        <div className="text-center">
          <div className="mx-auto h-16 w-16 bg-primary-500 rounded-full flex items-center justify-center">
            <User className="h-8 w-8 text-white" />
          </div>
          <h2 className="mt-6 text-3xl font-bold text-white">Create account</h2>
          <p className="mt-2 text-sm text-slate-400">
            Get started with your AI Dev Agent Orchestrator
          </p>
        </div>

        <div className="bg-slate-800/50 backdrop-blur rounded-xl p-8 shadow-xl border border-slate-700">
          <form className="space-y-6" onSubmit={handleSubmit}>
            {error && (
              <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-lg text-sm">
                {error}
                {error.toLowerCase().includes('already registered') && (
                  <div className="mt-2">
                    <Link to="/login" className="text-primary-400 hover:text-primary-300 font-medium underline">
                      Sign in instead
                    </Link>
                  </div>
                )}
              </div>
            )}

            <div>
              <label htmlFor="email" className="block text-sm font-medium text-slate-300 mb-2">
                Email address
              </label>
              <input
                id="email"
                name="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-3 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all"
                placeholder="you@example.com"
              />
            </div>

            <div>
              <div className="flex items-center justify-between mb-2">
                <label htmlFor="password" className="block text-sm font-medium text-slate-300">
                  Password
                </label>
                <button
                  type="button"
                  onClick={() => setShowRequirements(!showRequirements)}
                  className={`text-xs flex items-center gap-1 ${showRequirements ? 'text-primary-300' : 'text-primary-400 hover:text-primary-300'}`}
                >
                  <Info className="h-3 w-3" />
                  Requirements
                </button>
              </div>
              
              <div className="relative">
                <input
                  id="password"
                  name="password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="new-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-3 pr-10 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all"
                  placeholder="••••••••"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-300"
                >
                  {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>

              {showRequirements && (
                <div className="mt-2 p-3 bg-slate-900/30 rounded-lg border border-primary-500/30">
                  <p className="text-xs text-slate-300 mb-2 font-semibold">Password requirements:</p>
                  <ul className="text-xs text-slate-400 space-y-1">
                    <li className="flex items-start gap-2">
                      <span className="text-primary-400 mt-0.5">&bull;</span>
                      <span>Minimum <span className="text-slate-300">6 characters</span> (longer is better)</span>
                    </li>
                    <li className="flex items-start gap-2">
                      <span className="text-primary-400 mt-0.5">&bull;</span>
                      <span>Special characters <span className="text-slate-300">allowed</span>: @#$%^&amp;()_+-=[]{}|,.&lt;&gt;/?</span>
                    </li>
                    <li className="flex items-start gap-2">
                      <span className="text-primary-400 mt-0.5">&bull;</span>
                      <span>Uppercase, lowercase, numbers recommended for stronger password</span>
                    </li>
                  </ul>
                </div>
              )}
            </div>

            <div>
              <label htmlFor="confirmPassword" className="block text-sm font-medium text-slate-300 mb-2">
                Confirm password
              </label>
              <div className="relative">
                <input
                  id="confirmPassword"
                  name="confirmPassword"
                  type={showConfirmPassword ? 'text' : 'password'}
                  autoComplete="new-password"
                  required
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-3 pr-10 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent transition-all"
                  placeholder="••••••••"
                />
                <button
                  type="button"
                  onClick={() => setShowConfirmPassword(!showConfirmPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-300"
                >
                  {showConfirmPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>

            <button
              type="submit"
              disabled={loading}
              className="w-full flex items-center justify-center gap-2 bg-primary-500 hover:bg-primary-600 text-white font-semibold py-3 px-4 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loading ? (
                <div className="h-5 w-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              ) : (
                <>
                  Create account <ArrowRight className="h-4 w-4" />
                </>
              )}
            </button>
          </form>

          <div className="mt-6 text-center">
            <p className="text-sm text-slate-400">
              Already have an account?{' '}
              <Link to="/login" className="text-primary-400 hover:text-primary-300 font-medium">
                Sign in
              </Link>
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

export default Register;
