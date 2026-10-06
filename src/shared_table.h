#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

// The table that the carrier children share with the parent.
//
// It holds one lane for each carrier that the program can follow. A lane that
// holds a frequency is followed by one child. A lane that holds zero is free,
// and the parent gives it to the first in-span carrier that a control carrier
// grants for a clear call. A lane is never taken back: a carrier that the
// network uses once it will use again, and a retune costs the head of a call.
//
// The table also carries the clear-call grants themselves, one for each
// timeslot of each lane, which is what lets a child on a traffic carrier name
// the talkgroup that a control carrier announced.
class SharedTable {
public:
	struct Grant {
		uint32_t gssi;
		uint32_t issi;
		uint32_t control_hz;
	};

	// A child asks the parent for a lane on a frequency that no lane follows.
	struct Demand {
		uint32_t hz;
		uint32_t control_hz;
		uint32_t gssi;
	};

	// carriers seed the first lanes. lanes is the size of the whole pool, and
	// it never falls below the number of seed carriers.
	static SharedTable create(const std::vector<uint32_t>& carriers, size_t lanes);
	SharedTable(SharedTable&& other) noexcept;
	SharedTable& operator=(SharedTable&& other) noexcept;
	~SharedTable();

	SharedTable(const SharedTable&) = delete;
	SharedTable& operator=(const SharedTable&) = delete;

	size_t lanes() const;
	// The frequency that a lane follows, or 0 when the lane is free.
	uint32_t assigned(size_t lane) const;
	// Give the first free lane to hz. Returns the lane, or -1 when the pool is
	// full or another lane already follows that frequency.
	int assign(uint32_t hz);

	// A child wants a lane for d.hz. Returns false when the pool is full, so
	// the caller can report a call that this run cannot record.
	bool want(const Demand& d);
	// The parent takes one request. Returns false when there is none.
	bool take(Demand* d);

	bool publish(uint32_t carrier_hz, uint8_t tn_mask, int usage_marker, uint32_t gssi, uint32_t issi,
		     uint64_t sample, uint32_t control_hz = 0);
	bool invalidate(uint32_t carrier_hz, uint8_t tn, uint64_t sample);
	bool permits_clear(uint32_t carrier_hz, uint8_t tn, int local_encr, int dl_usage, uint64_t sample,
			   Grant* grant) const;

private:
	struct State;

	SharedTable(State* state, size_t map_size, int lock_fd);
	bool set_lock(short type) const;

	State* state = nullptr;
	size_t map_size = 0;
	int lock_fd = -1;
};
