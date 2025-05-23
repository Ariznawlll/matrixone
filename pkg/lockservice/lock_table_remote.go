// Copyright 2023 Matrix Origin
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package lockservice

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"os"
	"time"

	"github.com/matrixorigin/matrixone/pkg/common/log"
	"github.com/matrixorigin/matrixone/pkg/common/moerr"
	pb "github.com/matrixorigin/matrixone/pkg/pb/lock"
	"github.com/matrixorigin/matrixone/pkg/pb/timestamp"
	v2 "github.com/matrixorigin/matrixone/pkg/util/metric/v2"
	"github.com/matrixorigin/matrixone/pkg/util/trace"
	"go.uber.org/zap"
)

// remoteLockTable the lock corresponding to the Table is managed by a remote LockTable.
// And the remoteLockTable acts as a proxy for this LockTable locally.
type remoteLockTable struct {
	logger             *log.MOLogger
	removeLockTimeout  time.Duration
	serviceID          string
	bind               pb.LockTable
	client             Client
	bindChangedHandler func(pb.LockTable)
}

func newRemoteLockTable(
	serviceID string,
	removeLockTimeout time.Duration,
	binding pb.LockTable,
	client Client,
	bindChangedHandler func(pb.LockTable),
	logger *log.MOLogger,
) *remoteLockTable {
	logger = logger.With(zap.String("binding", binding.DebugString()))
	l := &remoteLockTable{
		logger:             logger,
		removeLockTimeout:  removeLockTimeout,
		serviceID:          serviceID,
		client:             client,
		bind:               binding,
		bindChangedHandler: bindChangedHandler,
	}
	return l
}

func (l *remoteLockTable) lock(
	ctx context.Context,
	txn *activeTxn,
	rows [][]byte,
	opts LockOptions,
	cb func(pb.Result, error),
) {
	v2.TxnRemoteLockTotalCounter.Inc()

	// FIXME(fagongzi): too many mem alloc in trace
	ctx, span := trace.Debug(ctx, "lockservice.lock.remote")
	defer span.End()

	logRemoteLock(l.logger, txn, rows, opts, l.bind)

	req := acquireRequest()
	defer releaseRequest(req)

	req.LockTable = l.bind
	req.Method = pb.Method_Lock
	req.Lock.Options = opts.LockOptions
	req.Lock.TxnID = txn.txnID
	req.Lock.ServiceID = l.serviceID
	req.Lock.Rows = rows

	// rpc maybe wait too long, to avoid deadlock, we need unlock txn, and lock again
	// after rpc completed
	txn.Unlock()
	resp, err := l.client.Send(ctx, req)
	txn.Lock()

	// txn closed
	if !bytes.Equal(req.Lock.TxnID, txn.txnID) {
		cb(pb.Result{}, ErrTxnNotFound)
		return
	}

	if err == nil {
		defer releaseResponse(resp)
		if err := l.maybeHandleBindChanged(resp); err != nil {
			logRemoteLockFailed(l.logger, txn, rows, opts, l.bind, err)
			cb(pb.Result{}, err)
			return
		}

		err = txn.lockAdded(l.bind.Group, l.bind, rows, l.logger)
		logRemoteLockAdded(l.logger, txn, rows, opts, l.bind)
		cb(resp.Lock.Result, err)
		return
	}

	// encounter any error, we also added lock to txn, because we need unlock on remote
	_ = txn.lockAdded(l.bind.Group, l.bind, rows, l.logger)
	logRemoteLockFailed(l.logger, txn, rows, opts, l.bind, err)
	// encounter any error, we need try to check bind is valid.
	// And use origin error to return, because once handlerError
	// swallows the error, the transaction will not be abort.
	if e := l.handleError(err, true); e != nil {
		err = e
	}
	cb(pb.Result{}, err)
}

func (l *remoteLockTable) unlock(
	txn *activeTxn,
	ls *cowSlice,
	commitTS timestamp.Timestamp,
	mutations ...pb.ExtraMutation) {
	logUnlockTableOnRemote(
		l.logger,
		txn,
		l.bind,
	)
	for {
		err := l.doUnlock(txn, commitTS, mutations...)
		if err == nil {
			return
		}

		logUnlockTableOnRemoteFailed(
			l.logger,
			txn,
			l.bind,
			err,
		)
		// unlock cannot fail and must ensure that all locks have been
		// released.
		//
		// handleError returns nil meaning bind changed, then all locks
		// will be released. If handleError returns any error, it means
		// that the current bind is valid, retry unlock.
		if err := l.handleError(err, false); err == nil {
			return
		}
	}
}

func (l *remoteLockTable) getLock(
	key []byte,
	txn pb.WaitTxn,
	fn func(Lock)) {
	for {
		lock, ok, err := l.doGetLock(key, txn)
		if err == nil {
			if ok {
				fn(lock)
				lock.close(notifyValue{})
			}
			return
		}

		// why use loop is similar to unlock
		if err = l.handleError(err, false); err == nil {
			return
		}
	}
}

func (l *remoteLockTable) doUnlock(
	txn *activeTxn,
	commitTS timestamp.Timestamp,
	mutations ...pb.ExtraMutation) error {
	ctx, cancel := context.WithTimeoutCause(context.Background(), defaultRPCTimeout, moerr.CauseDoUnlock)
	defer cancel()

	req := acquireRequest()
	defer releaseRequest(req)

	req.Method = pb.Method_Unlock
	req.LockTable = l.bind
	req.Unlock.TxnID = txn.txnID
	req.Unlock.CommitTS = commitTS
	req.Unlock.Mutations = mutations

	resp, err := l.client.Send(ctx, req)
	if err == nil {
		defer releaseResponse(resp)
		return l.maybeHandleBindChanged(resp)
	}
	return moerr.AttachCause(ctx, err)
}

func (l *remoteLockTable) doGetLock(key []byte, txn pb.WaitTxn) (Lock, bool, error) {
	ctx, cancel := context.WithTimeoutCause(context.Background(), defaultRPCTimeout, moerr.CauseDoGetLock)
	defer cancel()

	req := acquireRequest()
	defer releaseRequest(req)

	req.Method = pb.Method_GetTxnLock
	req.LockTable = l.bind
	req.GetTxnLock.Row = key
	req.GetTxnLock.TxnID = txn.TxnID

	resp, err := l.client.Send(ctx, req)
	if err == nil {
		defer releaseResponse(resp)
		if err := l.maybeHandleBindChanged(resp); err != nil {
			return Lock{}, false, err
		}

		wq := newWaiterQueue()
		wq.init(l.logger)
		lock := Lock{
			holders: newHolders(),
			waiters: wq,
			value:   byte(resp.GetTxnLock.Value),
		}
		lock.holders.add(txn)
		for _, v := range resp.GetTxnLock.WaitingList {
			w := acquireWaiter(v, "doGetLock", l.logger)
			lock.addWaiter(l.logger, w)
			w.close("doGetLock", l.logger)
		}
		return lock, true, nil
	}
	return Lock{}, false, moerr.AttachCause(ctx, err)
}

func (l *remoteLockTable) getBind() pb.LockTable {
	return l.bind
}

func (l *remoteLockTable) close() {
	logLockTableClosed(l.logger, l.bind, true)
}

func (l *remoteLockTable) handleError(
	err error,
	mustHandleLockBindChangedErr bool,
) error {
	if retryRemoteLockError(err) {
		err = moerr.NewBackendCannotConnectNoCtx(err.Error())
	}
	oldError := err
	// ErrLockTableBindChanged error must already handled. Skip
	if !mustHandleLockBindChangedErr && moerr.IsMoErrCode(err, moerr.ErrLockTableBindChanged) {
		return nil
	}

	// any other errors, retry.
	// Note. Since the cn where the remote lock table is located may
	// be permanently gone, we need to go to the allocator to check if
	// the bind is valid.
	new, err := getLockTableBind(
		l.client,
		l.bind.Group,
		l.bind.Table,
		l.bind.OriginTable,
		l.serviceID,
		l.bind.Sharding,
	)
	if err != nil {
		logGetRemoteBindFailed(l.logger, l.bind.Table, err)
		return oldError
	}
	if new.Changed(l.bind) {
		l.bindChangedHandler(new)
		return nil
	}
	return oldError
}

func retryRemoteLockError(err error) bool {
	if e, ok := err.(net.Error); ok && e.Timeout() {
		return true
	}
	if errors.Is(err, io.EOF) ||
		errors.Is(err, io.ErrUnexpectedEOF) ||
		errors.Is(err, os.ErrDeadlineExceeded) ||
		errors.Is(err, context.DeadlineExceeded) ||
		moerr.IsMoErrCode(err, moerr.ErrUnexpectedEOF) {
		return true
	}
	return false
}

func (l *remoteLockTable) maybeHandleBindChanged(resp *pb.Response) error {
	if resp.NewBind == nil {
		return nil
	}
	newBind := resp.NewBind
	l.bindChangedHandler(*newBind)
	return ErrLockTableBindChanged
}

func isRetryError(err error) bool {
	if moerr.IsMoErrCode(err, moerr.ErrBackendClosed) ||
		moerr.IsMoErrCode(err, moerr.ErrBackendCannotConnect) {
		return false
	}
	return true
}
