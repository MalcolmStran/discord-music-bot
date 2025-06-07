"""
Queue management for the music bot
"""

import logging
from collections import deque
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

class Queue:
    def __init__(self, max_size: int = 20):
        self.max_size = max_size
        self._queue = deque()
        self._history = deque(maxlen=10)  # Keep last 10 played songs
        
    def add(self, song: Dict[str, Any]) -> bool:
        """Add a song to the queue"""
        if len(self._queue) >= self.max_size:
            logger.warning(f"Queue is full (max {self.max_size})")
            return False
        
        self._queue.append(song)
        logger.info(f"Added song to queue: {song.get('title', 'Unknown')}")
        return True
    
    def get_next(self) -> Optional[Dict[str, Any]]:
        """Get the next song from the queue"""
        if not self._queue:
            return None
        
        song = self._queue.popleft()
        self._history.append(song)
        logger.info(f"Retrieved song from queue: {song.get('title', 'Unknown')}")
        return song
    
    def peek_next(self) -> Optional[Dict[str, Any]]:
        """Peek at the next song without removing it"""
        if not self._queue:
            return None
        return self._queue[0]
    
    def clear(self):
        """Clear the entire queue"""
        self._queue.clear()
        logger.info("Queue cleared")
    
    def remove(self, index: int) -> Optional[Dict[str, Any]]:
        """Remove a song at a specific index"""
        if 0 <= index < len(self._queue):
            song = self._queue[index]
            del self._queue[index]
            logger.info(f"Removed song at index {index}: {song.get('title', 'Unknown')}")
            return song
        return None
    
    def shuffle(self):
        """Shuffle the queue"""
        import random
        queue_list = list(self._queue)
        random.shuffle(queue_list)
        self._queue = deque(queue_list)
        logger.info("Queue shuffled")
    
    def move(self, from_index: int, to_index: int) -> bool:
        """Move a song from one position to another"""
        if not (0 <= from_index < len(self._queue) and 0 <= to_index < len(self._queue)):
            return False
        
        song = self._queue[from_index]
        del self._queue[from_index]
        self._queue.insert(to_index, song)
        logger.info(f"Moved song from {from_index} to {to_index}")
        return True
    
    def is_empty(self) -> bool:
        """Check if the queue is empty"""
        return len(self._queue) == 0
    
    def is_full(self) -> bool:
        """Check if the queue is full"""
        return len(self._queue) >= self.max_size
    
    def size(self) -> int:
        """Get the current queue size"""
        return len(self._queue)
    
    def remaining_space(self) -> int:
        """Get remaining space in the queue"""
        return self.max_size - len(self._queue)
    
    def current_queue(self) -> List[Dict[str, Any]]:
        """Get a copy of the current queue"""
        return list(self._queue)
    
    def get_history(self) -> List[Dict[str, Any]]:
        """Get the play history"""
        return list(self._history)
    
    def get_queue_info(self) -> Dict[str, Any]:
        """Get comprehensive queue information"""
        total_duration = sum(song.get('duration', 0) for song in self._queue)
        
        return {
            'size': len(self._queue),
            'max_size': self.max_size,
            'remaining_space': self.remaining_space(),
            'total_duration': total_duration,
            'is_empty': self.is_empty(),
            'is_full': self.is_full()
        }
