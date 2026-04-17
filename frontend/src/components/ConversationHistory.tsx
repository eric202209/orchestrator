import React, { useState, useEffect, useCallback } from 'react';
import axios from 'axios';

interface ConversationHistoryProps {
  sessionId: number;
  limit?: number;
}

interface ConversationMessage {
  id: number;
  role: string;
  content: string;
  timestamp: string;
  created_at?: string;
  metadata?: Record<string, unknown>;
}

export default function ConversationHistory({ sessionId, limit = 20 }: ConversationHistoryProps) {
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newMessage, setNewMessage] = useState('');
  const [sending, setSending] = useState(false);

  const fetchMessages = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);

      const response = await axios.get<{ messages: ConversationMessage[] }>(`/api/v1/context/conversation/${sessionId}`, {
        params: { limit },
      });

      setMessages(response.data.messages);
    } catch (err: unknown) {
      setError((err as { response?: { data?: { detail?: string } } }).response?.data?.detail || 'Failed to load conversation');
    } finally {
      setLoading(false);
    }
  }, [sessionId, limit]);

  useEffect(() => {
    fetchMessages();
  }, [fetchMessages]);

  const sendMessage = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newMessage.trim() || sending) return;

    try {
      setSending(true);

      await axios.post('/api/v1/context/conversation', {
        session_id: sessionId,
        role: 'user',
        content: newMessage.trim(),
      });

      setNewMessage('');
      await fetchMessages(); // Refresh messages
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } };
      setError(error.response?.data?.detail || 'Failed to send message');
    } finally {
      setSending(false);
    }
  };

  const getMessageIcon = (role: string) => {
    switch (role) {
      case 'user':
        return '👤';
      case 'assistant':
        return '🤖';
      case 'system':
        return '⚙️';
      default:
        return '💬';
    }
  };

  const getMessageColor = (role: string) => {
    switch (role) {
      case 'user':
        return 'bg-blue-500 text-white';
      case 'assistant':
        return 'bg-green-500 text-white';
      case 'system':
        return 'bg-gray-500 text-white';
      default:
        return 'bg-gray-500 text-white';
    }
  };

  if (loading) {
    return (
      <div className="bg-white p-6 rounded-lg shadow-md">
        <div className="animate-pulse space-y-4">
          {[1, 2, 3].map((i) => (
            <div key={i} className="flex space-x-3">
              <div className="w-8 h-8 bg-gray-200 rounded-full"></div>
              <div className="flex-1 space-y-2">
                <div className="h-4 bg-gray-200 rounded w-1/4"></div>
                <div className="h-3 bg-gray-200 rounded"></div>
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="bg-white p-6 rounded-lg shadow-md">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-gray-900">Conversation</h3>
        <span className="text-xs text-gray-500">{messages.length} messages</span>
      </div>

      {/* Messages List */}
      <div className="max-h-96 overflow-y-auto space-y-3 mb-4">
        {messages.length === 0 ? (
          <div className="text-center text-gray-500 py-8">
            <p>No conversation yet</p>
            <p className="text-sm">Start a conversation to track context</p>
          </div>
        ) : (
          messages.map((message) => (
            <div key={message.id} className="flex space-x-3">
              <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm ${getMessageColor(message.role)}`}>
                {getMessageIcon(message.role)}
              </div>
              <div className="flex-1">
                <div className="bg-gray-100 rounded-lg p-3">
                  <p className="text-sm text-gray-900">{message.content}</p>
                </div>
                <p className="text-xs text-gray-500 mt-1">
                  {new Date(message.created_at || message.timestamp).toLocaleTimeString()}
                </p>
              </div>
            </div>
          ))
        )}
      </div>

      {/* Error Message */}
      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-2 rounded-lg text-sm mb-3">
          {error}
        </div>
      )}

      {/* Input Form */}
      <form onSubmit={sendMessage} className="flex space-x-2">
        <input
          type="text"
          value={newMessage}
          onChange={(e) => setNewMessage(e.target.value)}
          placeholder="Add note to conversation..."
          className="flex-1 px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
          disabled={sending}
        />
        <button
          type="submit"
          disabled={sending || !newMessage.trim()}
          className="px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium"
        >
          {sending ? 'Sending...' : 'Add'}
        </button>
      </form>
    </div>
  );
}
