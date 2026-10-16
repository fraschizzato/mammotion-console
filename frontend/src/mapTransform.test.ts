import { describe, expect, it } from "vitest";
import {
  inverseMapPosition,
  resolveMapTransformOrigin,
  transformMapFeatures,
  transformMapPosition,
} from "./mapTransform";

const origin = { latitude: 45, longitude: 10 };

describe("shared map calibration", () => {
  it("rotates and offsets map coordinates in local metric space", () => {
    const point = transformMapPosition(
      [10.001, 45],
      { rotation_degrees: 90, offset_east_m: 4, offset_north_m: 3 },
      origin,
    );
    expect(point[0]).toBeCloseTo(10 + 4 / (111320 * Math.cos(Math.PI / 4)), 7);
    expect(point[1]).toBeCloseTo(
      45 + (0.001 * 111320 * Math.cos(Math.PI / 4) + 3) / 111320,
      7,
    );
    expect(inverseMapPosition(point, { rotation_degrees: 90, offset_east_m: 4, offset_north_m: 3 }, origin)).toEqual(
      expect.arrayContaining([expect.closeTo(10.001, 8), expect.closeTo(45, 8)]),
    );
  });

  it("uses one map centre as the fallback pivot and transforms every geometry", () => {
    expect(resolveMapTransformOrigin({}, [9, 44, 11, 46], null)).toEqual(origin);
    const source = {
      type: "FeatureCollection" as const,
      features: [
        {
          type: "Feature" as const,
          properties: {},
          geometry: { type: "LineString" as const, coordinates: [[10, 45], [10.001, 45]] },
        },
      ],
    };
    const result = transformMapFeatures(source, { offset_east_m: 10 }, origin);
    const geometry = result.features[0].geometry;
    expect(geometry && "coordinates" in geometry ? geometry.coordinates : null).toEqual([
      expect.arrayContaining([expect.closeTo(10 + 10 / (111320 * Math.cos(Math.PI / 4)), 8)]),
      expect.arrayContaining([expect.closeTo(10.001 + 10 / (111320 * Math.cos(Math.PI / 4)), 8)]),
    ]);
  });
});
