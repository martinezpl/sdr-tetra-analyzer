#include "shared_table.h"

#include <iostream>
#include <sys/wait.h>
#include <unistd.h>

static int fails;

static void check(bool ok, const char* name)
{
	if (!ok) {
		fails++;
		std::cerr << "FAIL " << name << "\n";
	}
}

static int child_status(pid_t pid)
{
	int status = 0;
	return waitpid(pid, &status, 0) == pid && WIFEXITED(status) ? WEXITSTATUS(status) : 255;
}

int main()
{
	const uint32_t control = 419962500;
	const uint32_t traffic = 420762500;
	SharedTable table = SharedTable::create({ control, traffic }, 2);
	SharedTable::Grant grant = {};

	check(!table.permits_clear(traffic, 1, -1, 10, 100, &grant), "absent grant denied");
	pid_t publisher = fork();
	if (publisher == 0)
		_exit(table.publish(traffic, 0x0a, 10, 1005, 3005001, 100) ? 0 : 1);
	check(publisher > 0 && child_status(publisher) == 0, "child published grant");
	check(!table.permits_clear(traffic, 1, -1, 10, 99, &grant), "future grant denied");
	check(table.permits_clear(traffic, 1, -1, 10, 100, &grant), "TN1 propagated");
	check(grant.gssi == 1005 && grant.issi == 3005001, "identities propagated");
	check(!table.permits_clear(traffic, 1, -1, 11, 100, &grant), "usage mismatch denied");
	check(!table.permits_clear(traffic, 1, -1, 3, 100, &grant), "nontraffic denied");
	check(table.permits_clear(traffic, 2, -1, 10, 100, &grant), "usage marker permits TN2");
	check(table.permits_clear(traffic, 3, -1, 10, 100, &grant), "TN3 propagated");
	check(table.permits_clear(traffic, 4, -1, 10, 100, &grant), "usage marker permits TN4");
	check(!table.permits_clear(traffic, 1, -1, 10, 100 + 30 * 36000 + 1, &grant),
	      "unused grant expired");

	for (int encr = 1; encr <= 3; encr++)
		check(!table.permits_clear(traffic, 1, encr, 10, 100, &grant),
		      "local encrypted state wins");
	check(table.permits_clear(traffic, 1, 0, 10, 100, &grant), "local clear state allowed");
	check(!table.publish(traffic, 0, 10, 0, 0, 110), "empty mask denied");
	check(!table.publish(traffic, 0x10, 10, 0, 0, 110), "malformed mask denied");
	check(!table.publish(415000000, 0x08, 10, 0, 0, 110), "unknown carrier denied");

	check(table.invalidate(traffic, 1, 200), "traffic exit invalidated");
	check(table.permits_clear(traffic, 1, -1, 10, 200, &grant), "other marked timeslot preserves grant");
	pid_t stale = fork();
	if (stale == 0)
		_exit(table.publish(traffic, 0x08, 10, 1005, 0, 150) ? 0 : 1);
	check(stale > 0 && child_status(stale) == 0, "delayed publisher completed");
	check(table.permits_clear(traffic, 1, -1, 10, 250, &grant),
	      "a stale grant cannot replace the newer timeslot grant");
	check(table.permits_clear(traffic, 3, -1, 10, 250, &grant), "other timeslot preserved");

	// The carrier pool. A free lane takes a frequency that a grant named, and
	// it is never taken back.
	{
		const uint32_t known = 419562500, fresh = 420562500, other = 420362500;
		SharedTable pool = SharedTable::create({ known }, 2);
		check(pool.lanes() == 2, "the pool holds one free lane");
		check(pool.assigned(0) == known, "the seed carrier sits in lane 0");
		check(pool.assigned(1) == 0, "the second lane starts free");
		check(!pool.publish(0, 0x08, 10, 1, 1, 100), "a free lane never matches frequency 0");

		check(pool.want({ fresh, known, 1234 }), "a free pool takes a request");
		check(pool.want({ fresh, known, 1234 }), "the same request twice is not an error");
		SharedTable::Demand d = {};
		check(pool.take(&d) && d.hz == fresh && d.gssi == 1234, "the request round trips");
		check(!pool.take(&d), "the queue holds each frequency once");

		check(pool.assign(fresh) == 1, "the free lane takes the carrier");
		check(pool.assigned(1) == fresh, "the lane follows it");
		check(pool.assign(fresh) < 0, "the same carrier is never assigned twice");
		check(pool.assign(other) < 0, "a full pool assigns nothing");
		check(!pool.want({ other, known, 1 }), "a full pool refuses a request");
		check(pool.want({ known, known, 1 }), "a carrier already followed needs no lane");

		// A lane must answer for the frequency it holds now, and for no other.
		SharedTable::Grant g = {};
		check(!pool.permits_clear(fresh, 1, -1, 10, 100, &g), "a new lane holds no grant");
		check(pool.publish(fresh, 0x08, 10, 42, 43, 100), "the new lane takes a grant");
		check(pool.permits_clear(fresh, 1, -1, 10, 100, &g) && g.gssi == 42,
		      "the new lane serves its own grant");
		check(!pool.publish(other, 0x08, 10, 1, 1, 100), "a carrier outside the pool is denied");
	}

	if (!fails)
		std::cout << "PASS shared table\n";
	return fails != 0;
}
