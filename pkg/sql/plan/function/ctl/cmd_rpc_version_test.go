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

package ctl

import (
	"fmt"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/matrixorigin/matrixone/pkg/clusterservice"
	"github.com/matrixorigin/matrixone/pkg/common/moerr"
	"github.com/matrixorigin/matrixone/pkg/common/morpc"
	"github.com/matrixorigin/matrixone/pkg/common/runtime"
	"github.com/matrixorigin/matrixone/pkg/defines"
	"github.com/matrixorigin/matrixone/pkg/pb/metadata"
	"github.com/matrixorigin/matrixone/pkg/queryservice"
	qclient "github.com/matrixorigin/matrixone/pkg/queryservice/client"
	"github.com/matrixorigin/matrixone/pkg/util/trace"
	"github.com/matrixorigin/matrixone/pkg/vm/process"
)

func TestHandleGetProtocolVersion(t *testing.T) {
	runtime.RunTest(
		"",
		func(rt runtime.Runtime) {
			var arguments struct {
				proc    *process.Process
				service serviceType
			}

			trace.InitMOCtledSpan()

			id := uuid.New().String()
			runtime.SetupServiceBasedRuntime(id, rt)

			addr := "127.0.0.1:7777"
			initRuntime([]string{id}, []string{addr})
			qs, err := queryservice.NewQueryService(id, addr, morpc.Config{})
			require.NoError(t, err)
			qt, err := qclient.NewQueryClient(id, morpc.Config{})
			require.NoError(t, err)

			arguments.proc = new(process.Process)
			arguments.proc.Base = &process.BaseProcess{}
			arguments.proc.Base.QueryClient = qt
			arguments.service = cn

			err = qs.Start()
			require.NoError(t, err)

			defer func() {
				qs.Close()
			}()

			ret, err := handleGetProtocolVersion(arguments.proc, arguments.service, "", nil)
			require.NoError(t, err)
			require.Equal(t, ret, Result{
				Method: GetProtocolVersionMethod,
				Data:   fmt.Sprintf("%s:%d", id, defines.MORPCLatestVersion),
			})
		},
	)
}

func TestHandleSetProtocolVersion(t *testing.T) {
	trace.InitMOCtledSpan()
	proc := new(process.Process)
	proc.Base = &process.BaseProcess{}
	id := uuid.New().String()
	addr := "127.0.0.1:7777"
	initRuntime([]string{id}, []string{addr})
	requireVersionValue(t, defines.MORPCLatestVersion)
	qs, err := queryservice.NewQueryService(id, addr, morpc.Config{})
	require.NoError(t, err)
	qt, err := qclient.NewQueryClient(id, morpc.Config{})
	require.NoError(t, err)
	proc.Base.QueryClient = qt

	cases := []struct {
		service serviceType
		version int64

		expectedErr error
	}{
		{service: cn, version: 1},
		{service: cn, version: 2},
		{service: tn, version: 1, expectedErr: moerr.NewInternalError(proc.Ctx, "no such tn service")},
	}

	err = qs.Start()
	require.NoError(t, err)
	defer func() {
		qs.Close()
	}()

	for _, c := range cases {
		var parameter string
		if c.service == tn {
			parameter = fmt.Sprintf("%d", c.version)
		} else {
			parameter = fmt.Sprintf("%s:%d", id, c.version)
		}

		ret, err := handleSetProtocolVersion(proc, c.service, parameter, nil)
		if c.expectedErr != nil {
			require.Equal(t, c.expectedErr, err)
			continue
		} else {
			require.NoError(t, err)
		}
		require.Equal(t, ret, Result{
			Method: SetProtocolVersionMethod,
			Data:   fmt.Sprintf("%s:%d", id, c.version),
		})
		requireVersionValue(t, c.version)
	}
}

func requireVersionValue(t *testing.T, version int64) {
	v, ok := runtime.ServiceRuntime("").GetGlobalVariables(runtime.MOProtocolVersion)
	require.True(t, ok)
	require.EqualValues(t, version, v)
}

func Test_transferToTN(t *testing.T) {

	rt := runtime.DefaultRuntime()
	runtime.SetupServiceBasedRuntime("", rt)
	mc := clusterservice.NewMOCluster(
		"",
		nil,
		3*time.Second,
		clusterservice.WithDisableRefresh(),
		clusterservice.WithServices(
			nil,
			[]metadata.TNService{
				{
					QueryAddress: "wrong address",
				},
			},
		),
	)
	defer mc.Close()
	rt.SetGlobalVariables(runtime.ClusterService, mc)

	qcli := &testQClient{}
	_, err := transferToTN(qcli, 0)
	assert.Error(t, err)
}
