// Copyright 2021 Matrix Origin
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

package model

import (
	"bytes"
	"testing"

	"github.com/matrixorigin/matrixone/pkg/container/types"
	"github.com/matrixorigin/matrixone/pkg/objectio"
	"github.com/matrixorigin/matrixone/pkg/vm/engine/tae/testutils"
	"github.com/stretchr/testify/assert"
)

func TestTree(t *testing.T) {
	defer testutils.AfterTest(t)()
	tree := NewTree()
	obj2 := objectio.NewObjectid()
	tree.AddObject(1, 2, &obj2, false)
	t.Log(tree.String())
	assert.Equal(t, 1, tree.TableCount())

	var w bytes.Buffer
	_, err := tree.WriteTo(&w)
	assert.NoError(t, err)

	tree2 := NewTree()
	_, err = tree2.ReadFromWithVersion(&w, MemoTreeVersion4)
	assert.NoError(t, err)
	t.Log(tree2.String())
	assert.True(t, tree.Equal(tree2))
}

func TestTableRecord_ReadOldVersion(t *testing.T) {
	defer testutils.AfterTest(t)()
	var w bytes.Buffer
	dbid := uint64(1)
	tableid := uint64(1)
	w.Write(types.EncodeUint64(&dbid))
	w.Write(types.EncodeUint64(&tableid))
	dataCnt := uint32(2)
	w.Write(types.EncodeUint32(&dataCnt))
	objid := objectio.NewObjectid()
	w.Write(objid[:])
	w.Write(objid[:])
	tombCnt := uint32(1)
	w.Write(types.EncodeUint32(&tombCnt))
	tombid := objectio.NewObjectid()
	w.Write(tombid[:])

	{
		copyw := w.Bytes()
		tree2 := NewTableTree(0, 0)
		assert.Panics(t, func() {
			tree2.ReadFromWithVersion(bytes.NewReader(copyw), MemoTreeVersion2)
		}, "not supported")
	}

	{
		tree2 := NewTableTree(0, 0)
		_, err := tree2.ReadFromWithVersion(&w, MemoTreeVersion3)
		assert.NoError(t, err)
		assert.Equal(t, dbid, tree2.DbID)
		assert.Equal(t, tableid, tree2.ID)

		var cnt uint32
		_, err = w.Read(types.EncodeUint32(&cnt))
		assert.Error(t, err) // EOF
	}

}
