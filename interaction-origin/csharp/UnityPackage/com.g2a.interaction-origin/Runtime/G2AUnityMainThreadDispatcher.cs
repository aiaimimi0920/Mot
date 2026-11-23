#nullable enable
using System;
using System.Collections.Concurrent;
using System.Threading;
using UnityEngine;

namespace G2A.InteractionOrigin.Unity
{
    /// <summary>
    /// Marshals Hub -> Origin execution callbacks safely onto Unity's main game thread.
    /// </summary>
    public sealed class G2AUnityMainThreadDispatcher : IEngineDispatcherPort
    {
        private readonly int _mainThreadId;
        private readonly ConcurrentQueue<Action> _queue = new();

        public G2AUnityMainThreadDispatcher()
        {
            _mainThreadId = Thread.CurrentThread.ManagedThreadId;
        }

        public void PumpMainThreadQueue()
        {
            while (_queue.TryDequeue(out var work))
            {
                work();
            }
        }

        public T RunOnGameThread<T>(Func<T> fn)
        {
            if (Thread.CurrentThread.ManagedThreadId == _mainThreadId)
            {
                return fn();
            }

            T? result = default;
            Exception? captured = null;
            using var done = new ManualResetEventSlim(false);
            _queue.Enqueue(() =>
            {
                try
                {
                    result = fn();
                }
                catch (Exception ex)
                {
                    captured = ex;
                }
                finally
                {
                    done.Set();
                }
            });
            done.Wait(TimeSpan.FromSeconds(5));
            if (captured != null) throw captured;
            return result!;
        }
    }
}
