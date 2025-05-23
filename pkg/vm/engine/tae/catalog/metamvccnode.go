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

package catalog

import (
	"fmt"
	"io"
	"unsafe"

	"github.com/matrixorigin/matrixone/pkg/container/types"
	"github.com/matrixorigin/matrixone/pkg/container/vector"
	"github.com/matrixorigin/matrixone/pkg/objectio"
	"github.com/matrixorigin/matrixone/pkg/vm/engine/tae/containers"
)

type MetadataMVCCNode struct {
	MetaLoc  objectio.Location
	DeltaLoc objectio.Location

	// For deltaloc from CN, it needs to ensure that deleteChain is empty.
	NeedCheckDeleteChainWhenCommit bool
}

func NewEmptyMetadataMVCCNode() *MetadataMVCCNode {
	return &MetadataMVCCNode{}
}

func (e *MetadataMVCCNode) CloneAll() *MetadataMVCCNode {
	node := &MetadataMVCCNode{
		MetaLoc:  e.MetaLoc,
		DeltaLoc: e.DeltaLoc,
	}
	return node
}

func (e *MetadataMVCCNode) CloneData() *MetadataMVCCNode {
	return &MetadataMVCCNode{
		MetaLoc:  e.MetaLoc,
		DeltaLoc: e.DeltaLoc,
	}
}

func (e *MetadataMVCCNode) String() string {

	return fmt.Sprintf("[MetaLoc=\"%s\",DeltaLoc=\"%s\"]",
		e.MetaLoc.String(),
		e.DeltaLoc.String())
}

// for create drop in one txn
func (e *MetadataMVCCNode) Update(un *MetadataMVCCNode) {
	if !un.MetaLoc.IsEmpty() {
		e.MetaLoc = un.MetaLoc
	}
	if !un.DeltaLoc.IsEmpty() {
		e.DeltaLoc = un.DeltaLoc
	}
}
func (e *MetadataMVCCNode) WriteTo(w io.Writer) (n int64, err error) {
	var sn int64
	if sn, err = objectio.WriteBytes(e.MetaLoc, w); err != nil {
		return
	}
	n += sn
	if sn, err = objectio.WriteBytes(e.DeltaLoc, w); err != nil {
		return
	}
	n += sn
	return
}

func (e *MetadataMVCCNode) ReadFromWithVersion(r io.Reader, ver uint16) (n int64, err error) {
	var sn int64
	if e.MetaLoc, sn, err = objectio.ReadBytes(r); err != nil {
		return
	}
	n += sn
	if e.DeltaLoc, sn, err = objectio.ReadBytes(r); err != nil {
		return
	}
	n += sn
	return
}

type ObjectMVCCNode struct {
	objectio.ObjectStats
}

func NewEmptyObjectMVCCNode() *ObjectMVCCNode {
	return &ObjectMVCCNode{
		ObjectStats: *objectio.NewObjectStats(),
	}
}

func NewObjectInfoWithMetaLocation(metaLoc objectio.Location, id *objectio.ObjectId) *ObjectMVCCNode {
	node := NewEmptyObjectMVCCNode()
	if metaLoc.IsEmpty() {
		objectio.SetObjectStatsObjectName(&node.ObjectStats, objectio.BuildObjectNameWithObjectID(id))
		return node
	}
	objectio.SetObjectStatsObjectName(&node.ObjectStats, metaLoc.Name())
	objectio.SetObjectStatsExtent(&node.ObjectStats, metaLoc.Extent())
	return node
}

func NewObjectInfoWithObjectID(id *objectio.ObjectId) *ObjectMVCCNode {
	node := NewEmptyObjectMVCCNode()
	objectio.SetObjectStatsObjectName(&node.ObjectStats, objectio.BuildObjectNameWithObjectID(id))
	return node
}

func NewObjectInfoWithObjectStats(stats *objectio.ObjectStats) *ObjectMVCCNode {
	return &ObjectMVCCNode{
		ObjectStats: *stats.Clone(),
	}
}

func (e *ObjectMVCCNode) CloneAll() *ObjectMVCCNode {
	obj := &ObjectMVCCNode{
		ObjectStats: *e.ObjectStats.Clone(),
	}
	return obj
}
func (e *ObjectMVCCNode) CloneData() *ObjectMVCCNode {
	return &ObjectMVCCNode{
		ObjectStats: *e.ObjectStats.Clone(),
	}
}
func (e *ObjectMVCCNode) String() string {
	if e == nil {
		return "[OBJ(nil)]"
	}
	return e.ObjectStats.String()
}
func (e *ObjectMVCCNode) Update(vun *ObjectMVCCNode) {
	e.ObjectStats = *vun.ObjectStats.Clone()
}
func (e *ObjectMVCCNode) WriteTo(w io.Writer) (n int64, err error) {
	var sn int
	if sn, err = w.Write(e.ObjectStats[:]); err != nil {
		return
	}
	n += int64(sn)
	return
}
func (e *ObjectMVCCNode) ReadFromWithVersion(r io.Reader, ver uint16) (n int64, err error) {
	var sn int
	if sn, err = r.Read(e.ObjectStats[:]); err != nil {
		return
	}
	n += int64(sn)
	return
}

func (e *ObjectMVCCNode) IsEmpty() bool {
	return e.Size() == 0
}

func (e *ObjectMVCCNode) AppendTuple(sid *types.Objectid, batch *containers.Batch) {
	batch.GetVectorByName(ObjectAttr_ObjectStats).Append(e.ObjectStats[:], false)
}

func ReadObjectInfoTuple(objs *vector.Vector, row int) (e *ObjectMVCCNode) {
	objBuf := objs.GetBytesAt(row)
	e = new(ObjectMVCCNode)
	e.ObjectStats.UnMarshal(objBuf)
	return
}

type ObjectNode struct {
	IsLocal  bool   // this object is hold by a localobject
	SortHint uint64 // sort object by create time, make iteration on object determined

	// for tombstone
	IsTombstone bool

	forcePNode  bool // not persisted, a flag to force ckp-replayed aobject to be created as a pnode
	nextVersion *ObjectEntry
	prevVersion *ObjectEntry
}

const (
	BlockNodeSize int64 = int64(unsafe.Sizeof(BlockNode{}))
)

func (node *ObjectNode) ReadFrom(r io.Reader) (n int64, err error) {
	_, err = r.Read(types.EncodeBool(&node.IsLocal))
	if err != nil {
		return
	}
	n += 1
	_, err = r.Read(types.EncodeUint64(&node.SortHint))
	if err != nil {
		return
	}
	n += 8
	_, err = r.Read(types.EncodeBool(&node.IsTombstone))
	if err != nil {
		return
	}
	n += 1
	return
}

func (node *ObjectNode) WriteTo(w io.Writer) (n int64, err error) {
	_, err = w.Write(types.EncodeBool(&node.IsLocal))
	if err != nil {
		return
	}
	n += 1
	_, err = w.Write(types.EncodeUint64(&node.SortHint))
	if err != nil {
		return
	}
	n += 8
	_, err = w.Write(types.EncodeBool(&node.IsTombstone))
	if err != nil {
		return
	}
	n += 1
	return
}

type BlockNode struct {
	state EntryState
}

func EncodeBlockNode(node *BlockNode) []byte {
	return unsafe.Slice((*byte)(unsafe.Pointer(node)), BlockNodeSize)
}

func (node *BlockNode) ReadFrom(r io.Reader) (n int64, err error) {
	if _, err = r.Read(EncodeBlockNode(node)); err != nil {
		return
	}
	n += BlockNodeSize
	return
}

func (node *BlockNode) WriteTo(w io.Writer) (n int64, err error) {
	if _, err = w.Write(EncodeBlockNode(node)); err != nil {
		return
	}
	n += BlockNodeSize
	return
}
