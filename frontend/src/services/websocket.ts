/**
 * WebSocket Service for Real-time Log Streaming
 * 
 * Implements heartbeat mechanism to prevent 5-minute disconnect timeouts.
 * Sends ping every 25 seconds (server pings at 30s) to maintain connection.
 * 
 * TIMEOUT FIX: Prevents automatic disconnection after 5 minutes of inactivity
 */

export interface WebSocketMessage {
  type: 'connected' | 'log' | 'status_update' | 'error';
  session_id?: number;
  session_instance_id?: string;
  level?: string;
  message?: string;
  timestamp?: string;
  status?: unknown;
}

export interface WebSocketOptions {
  onMessage?: (message: WebSocketMessage) => void;
  onError?: (error: Error) => void;
  onReconnect?: (attempt: number) => void;
  maxReconnectAttempts?: number;
  reconnectDelayMs?: number;
}

class WebSocketService {
  private ws: WebSocket | null = null;
  private heartbeatInterval: NodeJS.Timeout | null = null;
  private reconnectAttempts: number = 0;
  private readonly maxReconnectAttempts: number;
  private readonly reconnectDelay: number;
  
  private baseUrl: string;
  private sessionId: number | null = null;

  constructor(options: WebSocketOptions = {}) {
    this.maxReconnectAttempts = options.maxReconnectAttempts ?? 5;
    this.reconnectDelay = options.reconnectDelayMs ?? 5000;
    
    // Determine base URL (WebSocket uses ws:// instead of http://)
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.baseUrl = `${protocol}//${window.location.host}`;
    
    // Attach event handlers if provided
    if (options.onMessage) {
      this.onMessage = options.onMessage;
    }
    if (options.onError) {
      this.onError = options.onError;
    }
    if (options.onReconnect) {
      this.onReconnect = options.onReconnect;
    }
  }

  /**
   * Connect to WebSocket for a specific session
   */
  connect(sessionId: number, endpoint: 'logs' | 'status' = 'logs'): void {
    if (this.ws) {
      console.warn('WebSocket already connected, disconnecting first');
      this.disconnect();
    }

    this.sessionId = sessionId;
    const wsUrl = `${this.baseUrl}/api/v1/sessions/${sessionId}/${endpoint === 'logs' ? 'logs/stream' : 'status'}`;
    
    console.log(`Connecting to WebSocket: ${wsUrl}`);
    
    try {
      this.ws = new WebSocket(wsUrl);

      this.ws.onopen = () => {
        console.log('✅ WebSocket connected successfully');
        this.reconnectAttempts = 0;
        this.startHeartbeat();
      };

      this.ws.onmessage = (event) => {
        try {
          // Check if it's a raw ping/pong (not JSON)
          if (event.data === 'ping') {
            // Server sent ping, respond with pong
            this.ws?.send('pong');
            console.log('📡 Received ping from server, sending pong');
            return;
          }
          
          if (event.data === 'pong') {
            console.log('⏰ Received pong response from server');
            return;
          }

          // Parse JSON message
          const data: WebSocketMessage = JSON.parse(event.data);
          
          if (data.type === 'connected') {
            console.log(`✅ Connected to session ${sessionId}, heartbeat interval: ${data.heartbeat_interval || 30}s`);
          } else if (data.type === 'log') {
            this.onMessage?.(data);
          } else if (data.type === 'status_update') {
            this.onMessage?.(data);
          } else if (data.type === 'error') {
            console.error('❌ WebSocket error:', data.message);
            this.onError?.(new Error(data.message || 'WebSocket error'));
          }
        } catch (error) {
          console.error('Failed to parse WebSocket message:', error, event.data);
        }
      };

      this.ws.onclose = (event) => {
        console.log(`🔌 WebSocket disconnected: code=${event.code}, reason=${event.reason}`);
        this.stopHeartbeat();
        this.attemptReconnect(endpoint);
      };

      this.ws.onerror = (error) => {
        console.error('❌ WebSocket error:', error);
        this.onError?.(error as Error);
      };
    } catch (error) {
      console.error('Failed to create WebSocket connection:', error);
      this.onError?.(error as Error);
    }
  }

  /**
   * Send a message to the server
   */
  send(data: string | object): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('WebSocket is not connected, cannot send message');
      return;
    }

    const message = typeof data === 'string' ? data : JSON.stringify(data);
    this.ws.send(message);
  }

  /**
   * Start heartbeat mechanism to prevent timeout disconnects
   */
  private startHeartbeat(): void {
    // Send ping every 25 seconds (server pings at 30s, so we respond before that)
    console.log('🎵 Starting heartbeat mechanism (ping every 25s)');
    
    this.heartbeatInterval = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send('ping');
        console.log('⏰ Sending heartbeat ping to server');
      } else {
        console.warn('Heartbeat skipped: WebSocket not open');
      }
    }, 25000); // 25 seconds
  }

  /**
   * Stop heartbeat mechanism
   */
  private stopHeartbeat(): void {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
      console.log('🛑 Heartbeat mechanism stopped');
    }
  }

  /**
   * Attempt to reconnect with exponential backoff
   */
  private attemptReconnect(endpoint: 'logs' | 'status'): void {
    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++;
      const delay = this.reconnectDelay * this.reconnectAttempts;
      
      console.log(`🔄 Attempting to reconnect... (${this.reconnectAttempts}/${this.maxReconnectAttempts}) in ${delay/1000}s`);
      
      if (this.onReconnect) {
        this.onReconnect(this.reconnectAttempts);
      }

      setTimeout(() => {
        if (this.sessionId !== null) {
          this.connect(this.sessionId, endpoint);
        }
      }, delay);
    } else {
      console.error('❌ Max reconnection attempts reached. Please refresh the page.');
      this.onError?.(new Error('Max reconnection attempts reached'));
    }
  }

  /**
   * Disconnect from WebSocket
   */
  disconnect(): void {
    console.log('🔌 Disconnecting WebSocket...');
    
    this.stopHeartbeat();
    
    if (this.ws) {
      this.ws.close(1000, 'Client disconnect');
      this.ws = null;
    }
    
    this.sessionId = null;
  }

  /**
   * Check if connected
   */
  isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  /**
   * Get current connection state
   */
  getState(): 'connecting' | 'open' | 'closing' | 'closed' {
    if (!this.ws) return 'closed';
    
    switch (this.ws.readyState) {
      case WebSocket.CONNECTING:
        return 'connecting';
      case WebSocket.OPEN:
        return 'open';
      case WebSocket.CLOSING:
        return 'closing';
      default:
        return 'closed';
    }
  }

  // Event handler methods (can be overridden)
  onMessage: (message: WebSocketMessage) => void = () => {};
  onError: (error: Error) => void = () => {};
  onReconnect: (attempt: number) => void = () => {};
}

// Export singleton instance
export const websocketService = new WebSocketService();
export default websocketService;
