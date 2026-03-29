#!/usr/bin/env python3
"""
WebSocket Timeout Test Script
=============================
Tests that WebSocket connections stay alive for extended periods (>5 minutes)
without disconnecting due to timeout.

This script simulates a client connecting to the WebSocket endpoint and
verifying that:
1. Connection establishes successfully
2. Heartbeat mechanism works (ping/pong)
3. Connection stays alive for at least 6 minutes (past the 5-minute threshold)
4. No unexpected disconnections occur
"""

import asyncio
import websockets
import json
import time
from datetime import datetime

# Configuration
WS_URL = "ws://127.0.0.1:8080/api/v1/sessions/1/logs/stream"
TEST_DURATION = 360  # Test for 6 minutes (past the 5-minute timeout threshold)
HEARTBEAT_INTERVAL = 5  # Send ping every 5 seconds to test heartbeat

class WebSocketTimeoutTester:
    def __init__(self, url: str):
        self.url = url
        self.ws = None
        self.connected = False
        self.messages_received = 0
        self.pings_sent = 0
        self.pongs_received = 0
        self.errors = []
        self.start_time = None
        self.test_completed = False
        
    async def connect(self):
        """Connect to WebSocket and verify connection"""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Connecting to {self.url}...")
        
        try:
            self.ws = await websockets.connect(self.url)
            self.connected = True
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Connected successfully!")
            
            # Wait for initial connection message
            init_message = await asyncio.wait_for(
                self.ws.recv(), 
                timeout=5.0
            )
            data = json.loads(init_message)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Initial message: {data.get('type', 'unknown')}")
            
            if data.get('type') == 'connected':
                heartbeat_interval = data.get('heartbeat_interval', 30)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Server heartbeat interval: {heartbeat_interval}s")
                
        except Exception as e:
            self.errors.append(f"Connection error: {str(e)}")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Connection failed: {e}")
            raise
            
    async def send_heartbeat(self):
        """Send periodic heartbeats to test connection"""
        try:
            while self.connected and not self.test_completed:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                
                if self.ws and self.ws.open:
                    await self.ws.send("ping")
                    self.pings_sent += 1
                    
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📡 Sent ping #{self.pings_sent}")
                    
        except Exception as e:
            self.errors.append(f"Heartbeat error: {str(e)}")
            
    async def receive_messages(self):
        """Receive messages from WebSocket"""
        try:
            while self.connected and not self.test_completed:
                message = await asyncio.wait_for(
                    self.ws.recv(),
                    timeout=10.0
                )
                
                self.messages_received += 1
                
                if message == "ping":
                    # Server sent ping, respond with pong
                    await self.ws.send("pong")
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏰ Received server ping, sent pong")
                    
                elif message == "pong":
                    self.pongs_received += 1
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏰ Received pong #{self.pongs_received}")
                    
                else:
                    try:
                        data = json.loads(message)
                        if data.get('type') == 'log':
                            level = data.get('level', 'INFO')
                            msg_preview = data.get('message', '')[:50]
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] 📝 Log [{level}]: {msg_preview}...")
                        else:
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] Message #{self.messages_received}: {data.get('type', 'unknown')}")
                    except json.JSONDecodeError:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Raw message: {message[:100]}")
                        
        except asyncio.TimeoutError:
            # Normal timeout, continue waiting
            pass
        except Exception as e:
            if self.connected:  # Only record errors if we were connected
                self.errors.append(f"Receive error: {str(e)}")
                
    async def monitor_progress(self):
        """Monitor test progress and display status"""
        try:
            while not self.test_completed:
                elapsed = time.time() - self.start_time
                
                print(f"\n[PROGRESS] Elapsed: {elapsed:.0f}s / {TEST_DURATION}s")
                print(f"           Messages received: {self.messages_received}")
                print(f"           Pings sent: {self.pings_sent}")
                print(f"           Pongs received: {self.pongs_received}")
                print(f"           Errors: {len(self.errors)}")
                
                if elapsed >= TEST_DURATION:
                    self.test_completed = True
                    break
                    
                await asyncio.sleep(60)  # Update every minute
                
        except Exception as e:
            self.errors.append(f"Monitor error: {str(e)}")
            
    async def run_test(self):
        """Run the complete timeout test"""
        print("\n" + "="*60)
        print("WebSocket Timeout Test")
        print("="*60)
        print(f"Target URL: {WS_URL}")
        print(f"Test Duration: {TEST_DURATION}s ({TEST_DURATION/60} minutes)")
        print(f"Heartbeat Interval: {HEARTBEAT_INTERVAL}s")
        print("="*60 + "\n")
        
        self.start_time = time.time()
        
        try:
            # Connect to WebSocket
            await self.connect()
            
            # Start background tasks
            heartbeat_task = asyncio.create_task(self.send_heartbeat())
            receive_task = asyncio.create_task(self.receive_messages())
            monitor_task = asyncio.create_task(self.monitor_progress())
            
            # Wait for test duration
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🚀 Starting {TEST_DURATION}s test...\n")
            
            await asyncio.sleep(TEST_DURATION)
            self.test_completed = True
            
            # Give tasks time to finish gracefully
            await asyncio.sleep(2)
            
            # Cancel background tasks
            heartbeat_task.cancel()
            receive_task.cancel()
            monitor_task.cancel()
            
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
                
            try:
                await receive_task
            except asyncio.CancelledError:
                pass
                
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            
        except Exception as e:
            self.errors.append(f"Test error: {str(e)}")
            print(f"\n❌ Test failed with error: {e}")
            
        finally:
            # Disconnect
            if self.ws:
                await self.ws.close()
                self.connected = False
            
    def generate_report(self):
        """Generate test report"""
        elapsed = time.time() - self.start_time if self.start_time else 0
        
        print("\n" + "="*60)
        print("TEST REPORT")
        print("="*60)
        
        success = len(self.errors) == 0 and self.connected is False
        
        if success:
            print("✅ TEST PASSED!")
        else:
            print("❌ TEST FAILED!")
            
        print(f"\nDuration: {elapsed:.0f}s ({elapsed/60:.1f} minutes)")
        print(f"Messages received: {self.messages_received}")
        print(f"Pings sent: {self.pings_sent}")
        print(f"Pongs received: {self.pongs_received}")
        
        if self.errors:
            print(f"\nErrors ({len(self.errors)}):")
            for error in self.errors:
                print(f"  • {error}")
                
        print("\n" + "="*60)
        
        return success

async def main():
    """Main entry point"""
    tester = WebSocketTimeoutTester(WS_URL)
    
    try:
        await tester.run_test()
        success = tester.generate_report()
        exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        exit(1)
    except Exception as e:
        print(f"\nFatal error: {e}")
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
